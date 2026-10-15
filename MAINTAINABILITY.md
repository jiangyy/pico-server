# PicoPG Server 维护性优化清单

**基线**：183 commits · 34 个模块 · 4317 行非测试代码 · 1387 行测试 · 9 个测试文件全绿（Python 3.14）

**评估方式**：通读全部模块 + 用 `grep`/`wc` 度量重复与耦合。下面的每一条都带可复核的证据命令，不是主观感受。

**当前结论**：分层真实、持久化写法严谨、错误契约稳定。新增同类 feature 成本很低；修改**横切关注点**（分页、路由、鉴权、Log 命名）成本高，因为要改 4–7 处且无类型系统保护。数据层是清晰瓶颈但被 Protocol 隔离良好。

---

## 总览

| # | 项目 | 类型 | 影响面 | 改动成本 | 优先级 |
|---|---|---|---|---|---|
| 1 | 抽取 `read_all` / `find` 公共读取 | 去重 | 6 处副本 | 低 | **P0** |
| 2 | 建立测试与静态检查入口 | 工程基建 | 全仓 | 低 | **P0** |
| 3 | 授权规则改注册制 | 解耦 | 认证 ↔ 业务 | 中 | P1 |
| 4 | 拆分 `DraftAuthority.handle` | 可读性 | 核心判定逻辑 | 中 | P1 |
| 5 | 统一 JSON 校验原语 | 去重 | 5 处副本 | 低 | P1 |
| 6 | `FileReplicaLogStore` 改追加式 | 性能/容量 | 存储层 | 高 | P2 |
| 7 | worker 改增量游标 | 性能 | 后台消费者 | 中 | P2 |
| 8 | 路由表化 + handler 接口 Protocol 化 | 可读性 | HTTP 层 | 中 | P2 |

前 2 项不改任何协议、不改任何行为，可以立刻做。3–5 项不改协议。6–8 项是性能与可调试性投资，等真实负载或真实 bug 出现再做。

---

## P0-1 · 抽取 `read_all` / `find` 公共读取

### 问题

「分页读完一个 Log」这段循环在仓库里存在 **6 份**；「按 Position 取一条记录」存在 **3 份**。

```bash
grep -rn "^def _records\|^def _at" --include="*.py" apps src | grep -v test
# apps/server/draft/worker.py:71          _records
# apps/server/draft/authority.py:26       _records
# apps/server/draft/authority.py:39       _at
# apps/server/review/submissions.py:167   _records
# apps/server/review/submissions.py:180   _at
# apps/server/submission/worker.py:137    _records
# apps/server/submission/worker.py:150    _at
```

另有 2 处内联展开（未抽成函数）：

- `apps/server/review/comments.py:148`
- `apps/server/playground/catalog.py:144`

### 影响

改分页语义（加 page size 校验、改续页终止条件、加读取上限）要同步 6 个地方。
`_at()` 的三份实现都是 `store.has(position)` + 一次全量 `_records()` 扫描 —— 等于扫两遍。修一次性能要改三处。

### 做法

加到 `src/picopg/api/replica_log.py`（契约层，因为 `ReplicaLogStore` 就在那里）：

```python
PAGE_SIZE = 128


def read_all(store: ReplicaLogStore, log_id: str) -> tuple[ReplicatedRecord, ...]:
    """Drain one Log in page order. Raises ValueError on a stalled continuation."""
    records: list[ReplicatedRecord] = []
    after: Position | None = None
    while True:
        page = store.read(log_id, after, PAGE_SIZE)
        records.extend(page.records)
        if not page.has_more:
            return tuple(records)
        if not page.records:
            raise ValueError(f"{log_id} returned an empty continuation page")
        after = page.records[-1].position


def find(store: ReplicaLogStore, position: Position) -> ReplicatedRecord | None:
    """Fetch one Record by Position, or None when it is absent."""
    if not store.has(position):
        return None
    return next(
        (record for record in read_all(store, position.log_id) if record.id == position.record_id),
        None,
    )
```

然后 6 处替换为 `read_all(...)`，3 处替换为 `find(...)`，删掉本地 helper。

### ⚠️ 行为陷阱（务必注意）

现有 6 份副本全部 `raise ValueError`。而 `ReplicaLogError` **不是** `ValueError` 的子类：

```python
class ReplicaLogError(Exception): ...          # src/picopg/api/replica_log.py:22
```

`apps/server/draft/authority.py:195` 只捕获 `except (TypeError, ValueError)`。所以若 `read_all` 改抛 `ReplicaLogError`，`DraftAuthority` 将不再把它转成 `"invalid"` 结果，而是让它逃逸到 `ServerWorkerThread` 被当成「worker 失败，1 秒后无限重试」。

**验收标准**：`read_all` 必须 `raise ValueError` 以保持行为完全中立。若将来要统一成 `ReplicaLogError`，那是一次独立的、需要逐调用点核对 `except` 子句的改动。

### 成本

约 80 行删除、20 行新增。零协议变更、零行为变更。

---

## P0-2 · 建立测试与静态检查入口

### 问题

- `.github/` 不存在 —— 无 CI。
- `pyproject.toml` 只有 `[tool.setuptools]`，没有 ruff / mypy / ty / pytest 任何配置段。
- 9 个测试文件是 `python3 path.test.py` 手工逐个跑的形态，**没有聚合入口**，加文件后容易漏跑。
- `main.py:10-14` 手写 `sys.path.insert`，因此**必须在仓库根目录运行**，且没有 `console_scripts` entry point。

### 做法（两步，都是低风险）

**第一步：测试聚合脚本。** 每个测试文件都有 `main()` + `if __name__ == "__main__"`，所以最省事的聚合就是一个 shell 循环：

```sh
#!/bin/sh
# run-tests.sh — 在仓库根目录运行全部测试
set -e
cd "$(dirname "$0")"
fail=0
for test in $(find apps deploy -name '*.test.py' | sort); do
    printf '%-45s' "$test"
    if python3 "$test" >/tmp/picopg-test.log 2>&1; then
        echo "ok"
    else
        echo "FAIL"; cat /tmp/picopg-test.log; fail=1
    fi
done
exit $fail
```

**第二步：ruff 配置。** 只开真正会抓 bug 的规则集，不要一上来全开导致几百条噪音：

```toml
# pyproject.toml
[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]
```

`F`（未使用变量/导入）、`B`（bugbear，如可变默认参数）、`I`（导入排序）在这个仓库里立刻会有产出 —— 例如 `authentication/__init__.py` 等 5 个包的 `__init__.py` 是空文件，`submission/__init__.py` 为 0 字节。

### 验收标准

```bash
./run-tests.sh && ruff check .
```

两条命令的退出码为 0。接入 CI 时只跑这两条。

### 成本

半小时。收益是消除「漏跑测试」这一整类问题。

---

## P1-3 · 授权规则改注册制

### 问题

`apps/server/authentication/model.py:181-228`（48 行）的 `PrincipalLogAuthorization.access()` 手写 `log_id.split("/")` 解析，同时认识 draft / submission / playground 三类业务的 Log 命名：

```python
parts = log_id.split("/")
if ("drafts" in capabilities and len(parts) >= 5
        and parts[0] == "principal" and parts[1] == principal
        and parts[2] == "draft" and parts[3]):
    if len(parts) == 5 and parts[4] in ("decision", "comment"):
        return "read"
    ...
```

Log 命名的**构造函数**则分散在三个业务模块里：

```bash
grep -rln "principal/\|playground-releases\|/draft/\|/submission/" --include="*.py" apps src
# apps/server/draft/protocol.py      revision_log_id / request_log_id / decision_log_id
# apps/server/submission/logs.py     result_log / is_request_log / request_scope
# apps/server/review/comments.py     comment_log_id
# apps/server/playground/catalog.py  PLAYGROUND_RELEASES_LOG_ID
# apps/server/authentication/model.py  ← 又解析了一遍
```

### 影响

**认证模块本不该知道业务命名。** 加一个新 Log 类型必须回头改 `authentication/model.py`。改 Log 命名空间会同时打破 4 个文件，且编译器不会提醒 —— 这是纯字符串耦合。

### 做法

在每个业务模块里定义一个规则对象，认证层只负责遍历：

```python
# apps/server/draft/protocol.py
class DraftLogRule:
    capability = "drafts"

    def access(self, identity, log_id, directory, grants):
        parts = log_id.split("/")
        ...  # 现在 model.py:186-214 那段逻辑搬到这里
        return None


# apps/server/authentication/model.py
class PrincipalLogAuthorization:
    def __init__(self, sessions, directory, accounts=(), rules=()):
        self._rules = tuple(rules)
        ...

    def access(self, identity, log_id):
        ...  # 只做 capabilities / directory / explicit grant 检查
        for rule in self._rules:
            if rule.capability in capabilities:
                access = rule.access(identity, log_id, self._directory, grants)
                if access is not None:
                    return access
        return None
```

组合根 `apps/server/application.py` 传入规则列表：

```python
authorization = PrincipalLogAuthorization(
    sessions, directory, accounts,
    rules=(PlaygroundLogRule(), DraftLogRule(), SubmissionLogRule()),
)
```

### 验收标准

现有 `protocol.test.py` 的授权用例全绿，且 `grep -n "draft\|submission\|playground" apps/server/authentication/model.py` 只剩 `PRINCIPAL_CAPABILITIES` 里的能力名，没有路径解析。

### 成本

中等。逻辑是纯搬运，风险来自 `access()` 的分支顺序（`explicit grant` 优先于 capability）必须保持。

---

## P1-4 · 拆分 `DraftAuthority.handle`

### 问题

`apps/server/draft/authority.py:89-196`，108 行的单一方法，包含：

- 一个 `while True` 重试循环（118 行），内含 5 处 `_reject()` + `continue` 分支
- 3 种命令类型（`beginEditing` / `takeover` / `advance`）的条件嵌套
- 整个方法包在 `except (TypeError, ValueError)` 里

### 影响

全仓最难修改的方法。而且 **`except (TypeError, ValueError)` 会把真 bug 静默转成 `"invalid"` 结果** —— 结果是持久化的事实，所以调试时看不到任何 traceback。这是刻意的耐久性设计，代价是定位问题只能靠读代码。

### 做法

按命令类型分派，每种命令一个纯函数，各自返回「决策 dict 或拒绝原因」：

```python
def handle(self, request_position: Position) -> DraftResult:
    command, context = self._decode(request_position)   # 只在这里 try/except 数据校验
    if command is None:
        return context                                  # 已经是 DraftResult
    ...
    while True:
        decisions = read_all(self._store, decision_log)
        replayed = replayed_command(decisions, command.command_id)
        if replayed:
            return replay_result(replayed, request_position)
        ref = project_draft(decisions)
        outcome = self._decide(command, ref, context)   # 一个 dict: {"data": ...} 或 {"reject": reason}
        ...
```

关键是把 `except` 收窄到**数据校验边界**（`_decode`），而不是把整个判定循环包起来。判定逻辑抛异常应该是 500 而不是静默 invalid。

### 验收标准

`workers.test.py` 的 `draft_rejections_are_durable` 等用例全绿；`handle` 本体降到 40 行以内；故意在判定逻辑里插一个 `1/0` 能让测试炸出来（证明 bug 不再被吞）。

### 成本

中等偏高，需要先补几条覆盖 begin/takeover/advance/replay 的测试作为安全网。**建议先做 P0-1、P0-2 再做这一项。**

---

## P1-5 · 统一 JSON 校验原语

### 问题

```bash
grep -rn "^def _non_empty\|^def _position\|^def _text\|^def _unique_object\|^def _reject_constant" --include="*.py" apps src | grep -v test
```

| 原语 | 副本位置 |
|---|---|
| `_non_empty(value, label)` | `src/picopg/api/replica_log.py:38`（已有）· `authentication/registry.py:44` · `submission/worker.py:171` |
| `_text(value, label)` | `review/submissions.py:198` · `review/comments.py:52` |
| `_position(value, label)` | `review/submissions.py:189` · `review/comments.py:187` · `submission/worker.py:165` · `draft/protocol.py`（名为 `position`） |
| `_unique_object(pairs)` | `protocol.py:487` · `impl/file_replica_log.py:300` |
| `_reject_constant(value)` | `protocol.py:496` · `impl/file_replica_log.py:309` |

### 影响

这些是**协议级**的校验规则（「JSON 里不允许重复 key」「不允许 NaN」）。散落 2–4 份意味着它们可能悄悄分叉 —— 例如 `protocol.py` 的 `_reject_constant` 标注 `NoReturn`，`file_replica_log.py` 的标注 `None`，实际都 raise，但类型契约已经不一致了。

`submission/worker.py:171` 的 `_non_empty` 甚至是 `src/picopg/api/replica_log.py:38` 那个**已存在函数**的副本 —— 说明该函数只是没被导出。

### 做法

1. 把 `_non_empty` 从 `api/replica_log.py` 导出（去掉下划线），删掉 2 份副本。
2. 新增 `src/picopg/api/json_text.py`（或在 `replica_log.py` 里）放 `require_text(value, label)`、`require_position(value, label)`、`strict_json_loads(data)`。
3. `strict_json_loads` 内部固定使用 `object_pairs_hook=_unique_object, parse_constant=_reject_constant` —— 这两段在 `protocol.py:310` 和 `file_replica_log.py:156` 已经重复了一次完整调用。

### 验收标准

`grep -rn "object_pairs_hook" --include="*.py" apps src` 只命中 1 处（`strict_json_loads` 内部）。

### 成本

低，纯搬运。

---

## P2-6 · `FileReplicaLogStore` 改追加式存储

### 问题

三个 O(全部数据) 的操作：

| 位置 | 行为 | 复杂度 |
|---|---|---|
| `file_replica_log.py:169` `_write()` | 每次 append 重新序列化**整个** `replica-log.json` | O(全部记录) |
| `file_replica_log.py:47` `has()` | 线性扫描该 Log 全部记录 | O(Log 长度) |
| `file_replica_log.py:81` `read()` | 每页记录调 `from_value()`，**每条都重算一次 sha256** 自校验 | O(页大小) 次哈希 |

另外 `find()`/`_at()`（见 P0-1）先 `has()` 再全量扫，等于扫两遍。

`has()` + `_at()` 的组合被 `submission/worker.py` 对**每条**请求记录调用一次 → 单次 worker 轮询是 O(n²)。

### 影响

本地课堂规模（几百条记录）无影响。这是明确的容量天花板：单 Log 上万条后每次 append 都要全量重写。

**诚实说明**：docstring 里已标注 `shortcut: one process owns the directory; add a file lock before allowing multiple server processes`，但**没有标注「全量重写」**这一条，建议补上。

### 做法

分两步，第一步不需要换存储引擎：

1. **去掉重复哈希**：`_load()` 时校验一次并把 `ReplicatedRecord` 对象常驻内存（现在已经是了），但 `read()` 里不需要再 `from_value()` 重建 —— 直接返回内部对象即可。`ReplicatedRecord` 是 frozen + `MappingProxyType` 的不可变对象，外部拿到引用无法改它。这一步能砍掉读取路径上全部重复哈希。
2. **追加式落盘**：`replica-log.jsonl` 每行一条记录，append 只写一行 + fsync。启动时顺序重放建索引。需要定期 compaction（重写全量并原子替换）。

### ⚠️ 前提

必须确认没有外部消费者依赖当前**单文件 JSON** 格式。`deploy/backup.py` 是整目录打包，不受影响；但如果有测试直接读 `replica-log.json` 的 schema，需要一起改。

```bash
grep -rn "replica-log.json\|schema.*logs" --include="*.py" . | grep -v __pycache__
```

### 验收标准

新增一条测试：append 1000 条记录后，单次 append 的耗时不超过首次 append 的 3 倍。

### 成本

高。**等真实负载证明需要再做。**

---

## P2-7 · worker 改增量游标

### 问题

`apps/server/draft/worker.py:25` 的 `DraftRequestConsumer._processed` 是**纯内存** `set`：

```python
self._processed: set[tuple[str, str]] = set()
```

`run_once()`（第 27 行）每次唤醒都 `read_all` 重扫该 Log 的**全部**请求记录，靠第 34 行的 `if key in self._processed: continue` 跳过已处理的。

重启后 `_processed` 清空 → 全量重放。正确性靠 `DraftAuthority` 的 `replayed_command()` 保证（幂等），但每次启动都要重算全部历史。

`submission/worker.py:54` 同样：每次 `_run_log` 都 `_records(self._store, request_log)` 读完全部请求，再用 `completed` set 过滤。

### 影响

重启时间随历史记录线性增长。课堂规模无影响，但这是明确的扩展性上限。

### 做法

把「已处理位置」持久化到 Store 里，或者简单地在内存中记录**每个 Log 的最后处理 Position**，下次唤醒只读该 Position 之后的新记录：

```python
self._cursors: dict[str, Position] = {}

def run_once(self):
    for log_id in request_logs:
        page = self._store.read(log_id, self._cursors.get(log_id), 128)
        ...
        self._cursors[log_id] = page.records[-1].position
```

注意：`read(after=)` 在游标失效时抛 `UnknownPosition` —— 需要捕获并回退到全量重扫。

### ⚠️ 与 P0-1 的关系

`read_all` 解决的是「多处重复」，游标解决的是「每次重扫」。**先做 P0-1，再评估是否真需要游标。**

### 成本

中等。收益要在记录数上千后才体现。

---

## P2-8 · 路由表化 + handler 接口 Protocol 化

### 问题 A：5 份手写路由

```bash
grep -rn "def _route\|def _submission_route\|def _comment_route" --include="*.py" apps src
# apps/server/protocol.py:472         _route            len(parts) < 7 / parts[:5] != [...]
# apps/server/playground/http.py:92   _route            len(parts) == 6 / == 8
# apps/server/review/http.py:87       _comment_route    len(parts) != 8
# apps/server/review/http.py:101      _submission_route len(parts) == 6 / == 7
# apps/server/authentication/http.py  路径 in (...) 直接判断
```

全部是手写 `path.split("/")` + 索引比较。加端点要小心数 `len(parts) == 7`。

### 问题 B：跨模块调用私有方法

```bash
grep -o "handler\._[a-z_]*" apps/server/*/http.py | sort | uniq -c
# 24 handler._error          11 handler._send_json       4 handler._read_json
#  3 handler._cookie          2 handler._authenticated_replica  2 handler._admin_authorized
```

`_error` / `_send_json` / `_read_json` / `_cookie` / `_authenticated_replica` / `_admin_authorized` 是**事实上的内部接口**，但只用下划线表示，且靠 `if TYPE_CHECKING: from apps.server.application import PicoRequestHandler` 反向引用（`review/http.py:12`、`playground/http.py:11`、`authentication/http.py:10`）。

### 影响

重构 `ReplicaRequestHandler` 时这 3 个模块会一起炸。三处 `TYPE_CHECKING` 反向导入 `application.py` 也构成了潜在的循环依赖（现在被 `TYPE_CHECKING` 挡住了）。

### 做法

1. 在 `protocol.py` 定义一个 `RequestContext` Protocol，显式声明 dispatch 模块可以用哪些方法，把 `_error` 等公开化（或保留名字但纳入 Protocol）。
2. 路由改成表驱动：

```python
ROUTES = (
    ("GET",  re.compile(r"^/api/v1/catalog$"), handle_catalog),
    ("POST", re.compile(r"^/api/v1/playgrounds/(?P<playground>[^/]+)/versions$"), handle_publish),
    ...
)
```

### 成本

中等。属于「好看/好改」而非「修 bug」的改进，**优先级最低**。

---

## 明确不做（避免过度工程）

以下几项在评估中被考虑过，结论是**现在不做**：

- **引入 DI 框架 / 依赖注入容器**。`application.py` 作为唯一组合根已经够用（159 行，读得完）。框架会引入比它解决的问题更多的东西。
- **给每个 Protocol 加第二实现**。当前 `Authenticator` / `PackageAdapter` / `SubmissionTagWriter` 各只有一个真实实现。为了「可替换」而造测试替身是投机性抽象 —— 需要时再写。
- **换 PostgreSQL / ORM**。文件存储在单实例课堂规模下完全够用，且有正确的原子写入与备份工具。等真实负载证明瓶颈。
- **拆分 worker 到独立进程**。同进程 worker 消除了分布式一致性问题的全部来源。多实例前需要的是共享 Store + worker lease，那是大工程，不是优化。
- **加密码 / OAuth / 角色系统**。已明确声明为「不属于当前范围」（原 SERVER.md 的边界声明），不要在维护性重构里夹带。

---

## 建议执行顺序

1. **P0-2**（测试入口）—— 最小成本，是先做其他任何改动的前提。
2. **P0-1**（`read_all` / `find`）—— 纯删除，零行为变更，立刻减少 6 处维护点。
3. **P1-5**（校验原语）—— 同类纯搬运，可与 P0-1 合并成一次 PR。
4. **P1-3**（授权注册制）—— 解除最危险的跨模块字符串耦合。
5. **P1-4**（拆 `handle`）—— 需要先有测试安全网。
6. P2 项等真实负载数据。

第 1–3 步合起来可以在一次改动内完成，且**不触及任何协议**（`src/picopg/api/replica_log.py` 只新增函数和导出，不修改 `record_id_of` / `_value_bytes` / 异常层级）。

---

## 复核命令速查

```bash
# 重复分页读取副本数（目标：1）
grep -rn "^def _records\|^def _at" --include="*.py" apps src | grep -v test

# 严格 JSON 解析调用点（目标：1）
grep -rn "object_pairs_hook" --include="*.py" apps src

# 认证层是否仍解析业务路径（目标：不含 /draft/ 等）
grep -n "draft\|submission\|playground" apps/server/authentication/model.py

# 协议层是否泄漏业务知识（目标：0 命中）
grep -n "playground\|draft\|submission\|review" apps/server/protocol.py

# 测试总数与通过情况
find apps deploy -name '*.test.py' | wc -l && ./run-tests.sh
```

## 功能

1. 分发已发布的课程内容（客户端协议，见 `learn-python/apps/client/protocol.md`）。
2. 接收并保存学生交上来的作业。
3. 接收运维发布、下线的课程包。

## 对外接口

`/api/v1/` 下的每个请求都必须带 `X-Protocol-Version` 头（protocol.md §4）：
没有则 `400`，低于 Server 能服务的最低版本则 `426 client_too_old`。
`/healthz` 不需要。头里放不下 payload——目录请求没有参数，读一个文件只是 `GET`。

只读，匿名：

| 方法 | 路径 | 返回 |
| --- | --- | --- |
| GET | `/healthz` | `ok` |
| GET | `/api/v1/catalog` | `{upto_seq, protocol_version, minimum_client_protocol_version, playgrounds:[{playground_id, current_version_id}]}` |
| GET | `/api/v1/playgrounds/{id}/versions/{vid}/manifest` | `{playground_id, version_id, files:[{path,size,sha256}]}` |
| GET | `/api/v1/playgrounds/{id}/versions/{vid}/files/{path}` | 原始字节 + `X-Content-SHA256` |

客户端唯一的写通道，匿名：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/sync` | `{pending_events, after_seq}` → `{event_results, entries, upto_seq, protocol_version, minimum_client_protocol_version}` |

`pending_events` 按顺序处理：`event_id` 已存在就重放存量结果，否则交给该 kind 的处理器。
目前客户端能提交的只有 `submit_code`；`publish_playground` / `archive_playground`
由运维的端点产生，客户端提交它们会被逐条拒成 `invalid_event`。
未知 `kind` 不是错误：协议把 kind 当开放字符串，所以它单独被拒成 `unknown_kind`，同一轮其余事件照常处理。

需要 operator token（`Authorization: Bearer …`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/playgrounds` | `{id}` 登记一门课（409 = 已存在） |
| POST | `/api/v1/playgrounds/{id}/versions` | ZIP + `Idempotency-Key` + `X-Base-Version-Id`，返回 `{status, version_id}` |
| DELETE | `/api/v1/playgrounds/{id}` | 下线，返回 `{status}` |
| POST | `/api/v1/playgrounds/{id}/restore` | 重新上线，返回 `{status}` |
| GET | `/api/v1/submissions` | 分页列出交上来的作业 |
| GET | `/api/v1/submissions/{id}` | 取回一份作业的文件 |

`PublishResult.status`：`accepted` / `no_op` / `conflict` / `playground_stopped`。

发布必须带 `X-Base-Version-Id`（protocol.md §2）：Server 只在这个 Version 仍是当前版本时
才接受，否则回 `conflict` 并且什么都不写。没有它，两个人同时发同一门课，后发的会静默
覆盖先发的。首次发布（这门课还没有 Version）必须**不带**——带了就是 `conflict`。
缺失是 `400`，不是 `conflict`：那是畸形请求，不是过期的 base。
`ArchiveResult.status`：`accepted` / `no_op`。

下线之后**不能**直接重新发布（`state != active` → `409 playground_stopped`，
protocol.md §2），所以让课程回来是 `POST .../restore`：它把 `state` 改回 active，并为
当前 Version 记一条 `publish_playground` 事件——客户端就是靠这条事件知道课程回来了，
内容日志没有单独的"恢复"类型。`restore` 是 protocol.md 明确留的空档（它说 Publisher
不该靠重试同一个 PublishRequest 解决，但没定义该怎么做），不是协议里已有的操作。

## 存储

`data/app.db`：

- `playgrounds` —— 每门课一行：`state`（active/stopped）与 `current_version_id`。
- `events` —— 每个被处理过的事件一行，主键就是客户端的 `event_id`，同时存下原样重放的响应。
- `content_log` —— 只收 `publish_playground` 与 `archive_playground`，`seq` 即客户端游标。
- `submissions` —— 交上来的作业，只追加，文件正文在 `files_json`。

`data/picopg.git` —— 一个 bare 仓库，每门课一条 ref（`refs/heads/playgrounds/<id>`）。
一个 Version 就是一个 commit，内容不可变、天然去重。

## 不变量

1. **Archive 只改状态。** 下线把 `state` 置为 `stopped` 并记一条事件，ref 与 commit 都保留，
   所以已经装在学生机器上的版本仍然可读。
2. **事件表是唯一去重源。** 重放同一个 `event_id` 返回存下的响应，不会产生第二条记录。
3. **事务只由最外层拥有。** `events.py` / `playgrounds.py` 里的函数自己不开事务也不提交，
   一轮 sync 因此是一个整体：要么全部落库，要么一条都不落。
4. **学生姓名在文件里。** `submit_code` 的 payload 没有 `student`
   （见 `protocol.md`），服务端从 `work/main.py` 首行的 `# 姓名：… 班级：…` 读出。
5. **读取全公开。** 没有会话、没有角色、没有请求级判断；只有写操作要 token。
6. **目录只读数据库。** 每门课的 `current_version_id` 由 publish 写在同一次事务里，
   启动时补齐旧行（`playgrounds.backfill_current_versions`）。catalog 不再对每行跑
   `git rev-parse`：300 门课从 2.2 秒降到 5 毫秒。**代价**是一行若没有这个值就不出现在
   目录里——所以任何写 `playgrounds` 行的代码都必须维护它。

## 已知上限

catalog 是一个整体响应，客户端单次读取上限 1 MiB，每条约 89 字节，即**约 11800 门课**。
到那个量级需要分页，而 protocol.md 的 `CatalogRequest` 是无参数的，所以分页要同时改协议。
当前量级远远不到。

## 运维

```bash
# 启动（token 至少 16 字节，否则拒绝启动）
PICOPG_OPERATOR_TOKEN=... python3 apps/server/server.py

# 发一门课 / 下线
node scripts/publish.mjs <id> && node scripts/upload.mjs <id>   # 在 learn-python 里
node scripts/retire.mjs <id>
```

`deploy/picopg.service` 是 systemd 单元，工作目录 `~/picopg`，只允许写 `data/`。

## 代码布局

- `api_handler.py` —— HTTP 管道：有界读体、响应编码、错误词汇、协议版本闸门、
  operator token、每请求一条数据库连接。不知道 Playground / bundle / Git / 作业的存在，
  所以只能按形状理解，端点也无法借它绕到存储。
- `server.py` —— 路由分发与 10 个 handler。
- `routes.py` —— 所有 URL 形状与 method 表。纯函数，不碰数据库或仓库，
  因此可以脱离服务器直接测（`test_routes.py`）。
- 其余模块各管一个领域：`bundles`（ZIP 校验）、`repository`（Git）、
  `manifests`（manifest 与单文件）、`events`（事件与日志）、`sync`（一轮同步）、
  `playgrounds`（状态与发布/下线）、`submissions`（作业）。

## 测试

```bash
python3 -m unittest discover -s apps/server -p "test_*.py"

# 端到端：真服务器 + learn-python 的客户端模块（未设置就跳过）
PICOPG_CLIENT_DIR=../learn-python/apps/client \
    python3 -m unittest discover -s apps/server -p "test_*.py"
```

在 `learn-python` 那一侧：`npm test` 跑全部（含 client），
`npm run test:electron` 另外跑真 Electron 冒烟（需要先 `npm run build:client`）。

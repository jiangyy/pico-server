## 功能

1. 分发已发布的课程内容（客户端协议，见 `learn-python/apps/client/protocol.md`）。
2. 接收并保存学生交上来的作业。
3. 接收运维发布、下线的课程包。

## 对外接口

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
| POST | `/api/v1/playgrounds/{id}/versions` | ZIP + `Idempotency-Key`，返回 `{status, version_id}` |
| DELETE | `/api/v1/playgrounds/{id}` | 下线，返回 `{status}` |
| GET | `/api/v1/submissions` | 分页列出交上来的作业 |
| GET | `/api/v1/submissions/{id}` | 取回一份作业的文件 |

`PublishResult.status`：`accepted` / `no_op` / `conflict` / `playground_stopped`。
`ArchiveResult.status`：`accepted` / `no_op`。

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

## 运维

```bash
# 启动（token 至少 16 字节，否则拒绝启动）
PICOPG_OPERATOR_TOKEN=... python3 apps/server/server.py

# 发一门课 / 下线
node scripts/publish.mjs <id> && node scripts/upload.mjs <id>   # 在 learn-python 里
node scripts/retire.mjs <id>
```

`deploy/picopg.service` 是 systemd 单元，工作目录 `~/picopg`，只允许写 `data/`。

## 测试

```bash
python3 -m unittest discover -s apps/server -p "test_*.py"

# 端到端：真服务器 + learn-python 的客户端模块（未设置就跳过）
PICOPG_CLIENT_DIR=../learn-python/apps/client \
    python3 -m unittest discover -s apps/server -p "test_*.py"
```

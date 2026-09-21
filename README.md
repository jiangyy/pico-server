# Picopg

## 功能

1. 分发已发布的课程内容（客户端协议，见 `learn-python/apps/client/protocol.md`）。
2. 接收并保存学生交上来的作业。
3. 接收运维发布、下线的课程包。

## 对外接口

`/api/v1/` 下的每个请求都必须带 `X-Protocol-Version` 头，没有则 `400`，低于 Server 能服务的最低版本则 `426 client_too_old`。

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

需要 operator token（`Authorization: Bearer …`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/playgrounds` | `{id}` 登记一门课（409 = 已存在） |
| POST | `/api/v1/playgrounds/{id}/versions` | ZIP + `Idempotency-Key` + `X-Base-Version-Id`，返回 `{status, version_id}` |
| DELETE | `/api/v1/playgrounds/{id}` | 下线，返回 `{status}` |
| GET | `/api/v1/submissions` | 分页列出交上来的作业 |
| GET | `/api/v1/submissions/{id}` | 取回一份作业的文件 |

## 存储

`data/app.db`：

- `playgrounds` —— 每门课一行：`state`（active/stopped）与 `current_version_id`。
- `events` —— 每个被处理过的事件一行，主键就是客户端的 `event_id`，同时存下原样重放的响应。
- `content_log` —— 只收 `publish_playground` 与 `archive_playground`，`seq` 即客户端游标。
- `submissions` —— 交上来的作业，只追加，文件正文在 `files_json`。

`data/picopg.git` —— 一个 bare 仓库，每门课一条 ref（`refs/heads/playgrounds/<id>`）。
一个 Version 就是一个 commit，内容不可变、天然去重。

`data/submissions/` —— 交上来的作业的人可读副本，一行记录对应一个目录：

```
data/submissions/<班级>/<姓名>/<playgroundId>[-N]/work/main.py
                                                   /submission.json
```

`-N` 是同一个人对同一门课再交一次时的编号；`submission.json` 记着这份是谁的、基于
哪个 Version。学生名来自请求（`work/main.py` 首行），所以每一段都做了过滤——名字
不能变成路径。**真正的记录是数据库**，这份副本可以整份删掉再从 API 重新读。
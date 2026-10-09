# Picopg

## 功能

1. 分发已发布的课程内容（设计见 `learn-python/apps/client/core.md` 与
   `implementation.md`）。
2. 接收运维发布、下线的课程包。

学生交作业的通道已经删掉：一次提交在这套设计里是「确认一次日志同步」，服务端只据此造
一个不可变 checkpoint，不需要自己解释文件内容，也不需要第二份姓名/班级目录。新的写入
协议定下来之前，这里只有发布和读取。

## 对外接口

`/api/v1/` 下的每个请求都必须带 `X-Protocol-Version` 头，没有则 `400`，低于 Server 能服务的最低版本则 `426 client_too_old`。

只读，匿名：

| 方法 | 路径 | 返回 |
| --- | --- | --- |
| GET | `/healthz` | `ok` |
| GET | `/api/v1/catalog` | `{protocol_version, minimum_client_protocol_version, playgrounds:[{playground_id, current_version_id}], assignments:[{assignment_id, current_version_id, title}]}` |
| GET | `/api/v1/playgrounds/{id}/versions/{vid}/manifest` | `{playground_id, version_id, files:[{path,size,sha256}]}` |
| GET | `/api/v1/playgrounds/{id}/versions/{vid}/files/{path}` | 原始字节 + `X-Content-SHA256` |

需要 operator token（`Authorization: Bearer …`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/playgrounds` | `{id}` 登记一门课（409 = 已存在）。可选：发布到不存在的 id 也会创建它 |
| POST | `/api/v1/playgrounds/{id}/versions` | ZIP + `Idempotency-Key` + `X-Base-Version-Id`，返回 `{status, version_id}` |
| POST | `/api/v1/assignments/{id}/versions` | Markdown + `Idempotency-Key` + `X-Base-Version-Id`，返回 `{status, version_id}`；第一行必须是 `# 标题` |
| DELETE | `/api/v1/assignments/{id}` | 取消发布，返回 `{status}` |
| DELETE | `/api/v1/playgrounds/{id}` | 下线，返回 `{status}` |
| POST | `/api/v1/playgrounds/{id}/restore` | 重新上线，返回 `{status}` |

发布的 `status`：`accepted` / `no_op` / `conflict` / `playground_stopped`。对一个还不存在的
id 发布就是创建它（`base` 必须为空，否则 `conflict`）；已有 Version 时必须带 `base`，
缺了是 `400`，过时是 `409 conflict`。`conflict` 表示
`X-Base-Version-Id` 已经过时，这次什么都没有写；`playground_stopped` 表示这门课已下线，
所以不能靠重新发布让它回来——用上面的 `restore`。

## 存储

`data/app.db`：

- `playgrounds` —— 每门课一行：`state`（active/stopped）与 `current_version_id`。
- `assignments` —— 每道作业一行：`state`、`current_version_id`，外加从内容派生的 `title`。
- `idempotency_results` —— operator 发布端点的 `Idempotency-Key` 重放结果。

`data/picopg.git` —— 一个 bare 仓库，一个 Version 就是一个 commit，内容不可变、天然去重。
课程的历史在 `refs/picopg/history/<playground_id>`，作业的历史在 `refs/picopg/assignment/<assignment_id>`。
两类 ref：

- `refs/picopg/history/<playground_id>` —— 稳定的可达性根。它**从数据库的
  `current_version_id` 收敛**，不领先数据库；Archive 不删它。
- `refs/picopg/pending/<publish_id>` —— 一次发布在"commit 已建、数据库还没表态"之间的
  临时保护，防止那时被 Git GC 收走。`publish_id` 只是这次尝试的恢复标识，不是领域事件。
  发布结束就删；启动时扫描清理。

Publish 因此是三阶段：**Prepare**（建 commit + pending ref）→ 一个短事务（CAS
`current_version_id`）→ **Finalize**（把 history ref 收敛到数据库说的当前版本、删
pending ref）。Git 不在数据库事务里；数据库没答应之前 history ref 不动。中途崩溃由
启动扫描恢复：它拿每个 pending ref 指向的 Version 去问数据库"哪个 Playground 把它当成
当前版本"——有就收敛 history，没有就丢掉这个 ref。数据库没有接受过的 Version 丢掉，
接受过的把 history ref 补上。
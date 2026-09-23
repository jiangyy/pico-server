# Picopg

## 功能

1. 分发已发布的课程内容（设计见 `learn-python/apps/client/core.md` 与
   `implementation.md`）。
2. 接收并保存学生交上来的作业。
3. 接收运维发布、下线的课程包。

## 对外接口

`/api/v1/` 下的每个请求都必须带 `X-Protocol-Version` 头，没有则 `400`，低于 Server 能服务的最低版本则 `426 client_too_old`。

只读，匿名：

| 方法 | 路径 | 返回 |
| --- | --- | --- |
| GET | `/healthz` | `ok` |
| GET | `/api/v1/catalog` | `{protocol_version, minimum_client_protocol_version, playgrounds:[{playground_id, current_version_id}]}` |
| GET | `/api/v1/playgrounds/{id}/versions/{vid}/manifest` | `{playground_id, version_id, files:[{path,size,sha256}]}` |
| GET | `/api/v1/playgrounds/{id}/versions/{vid}/files/{path}` | 原始字节 + `X-Content-SHA256` |

客户端唯一的写入口，匿名：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/submissions` | `{submission_id, playground_id, version_id, files}` → `201 {status:"accepted", submission}` / `200 {status:"existing_same", submission}`；拒绝是 `400 invalid_submission`、`404 invalid_version`、`409 playground_stopped`、`409 submission_conflict` |

`submission_id` 由客户端生成：同名重发得到同一个结果，同名不同内容被拒绝。学生名不
在请求里，服务器从 `work/main.py` 首行（`# 姓名：… 班级：…`）读出来，作为老师那份
可读副本的署名。

需要 operator token（`Authorization: Bearer …`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/playgrounds` | `{id}` 登记一门课（409 = 已存在）。可选：发布到不存在的 id 也会创建它 |
| POST | `/api/v1/playgrounds/{id}/versions` | ZIP + `Idempotency-Key` + `X-Base-Version-Id`，返回 `{status, version_id}` |

发布的 `status`：`accepted` / `no_op` / `conflict` / `playground_stopped`。对一个还不存在的
id 发布就是创建它（`base` 必须为空，否则 `conflict`）；已有 Version 时必须带 `base`，
缺了是 `400`，过时是 `409 conflict`。`conflict` 表示
`X-Base-Version-Id` 已经过时，这次什么都没有写；`playground_stopped` 表示这门课已下线，
所以不能靠重新发布让它回来——用下面的 `restore`。
| DELETE | `/api/v1/playgrounds/{id}` | 下线，返回 `{status}` |
| POST | `/api/v1/playgrounds/{id}/restore` | 重新上线，返回 `{status}` |
| GET | `/api/v1/submissions` | 分页列出交上来的作业 |
| GET | `/api/v1/submissions/{id}` | 取回一份作业的文件 |

## 存储

`data/app.db`：

- `playgrounds` —— 每门课一行：`state`（active/stopped）与 `current_version_id`。
- `submissions` —— 交上来的作业，只追加，文件正文在 `files_json`；`id` 就是客户端给的
  `submission_id`，唯一约束就是幂等。
- `idempotency_results` —— operator 发布端点的 `Idempotency-Key` 重放结果（它只服务那
  一个端点，不是通用事件表）。

`data/picopg.git` —— 一个 bare 仓库，一个 Version 就是一个 commit，内容不可变、天然去重。
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

`data/submissions/` —— 交上来的作业的人可读副本，一行记录对应一个目录：

```
data/submissions/<班级>/<姓名>/<playgroundId>[-N]/work/main.py
                                                   /submission.json
```

`-N` 是同一个人对同一门课再交一次时的编号；`submission.json` 记着这份是谁的、基于
哪个 Version。学生名来自请求（`work/main.py` 首行），所以每一段都做了过滤——名字
不能变成路径。真正的记录是数据库，这份副本可以整份删掉再从 API 重新读。
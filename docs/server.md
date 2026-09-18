# Server 契约

只做分发和接收提交，不执行学生代码，不判断调用者是谁。

## 存储

```text
data/
  picopg.git/  私有裸仓库（只有 playgrounds ref）
  app.db       数据库：playgrounds、idempotency_results、submissions
  generated/   可重建缓存：catalog、manifest index、bootstrap、artifact index
  artifacts/   不可变 runtime/EXE，按 version 目录存放
  runtime/     课程页面按需加载的共享运行时
  submissions/ 收上来的作业：<班级>/<姓名>/<时间>-<id8>/work/**
```

`submissions/` 是数据库行的副本，给批改的人看：一份作业一个目录，里面是学生交的
`work/**` 加一个 `submission.json`（id、student、playgroundId、commitId、createdAt、
文件清单）。学生自报的名字是这条路径里唯一由调用者控制的部分，所以它被切成安全
的单段文件名；同名作业重交是**同一个人的新目录**（时间戳不同），精确重试则写到同一处。

## 读取（公开）

| 路径 | 内容 |
| --- | --- |
| `GET /api/v1/catalog` | 已发布 Playground 的当前 commit，分页 |
| `GET /api/v1/bootstrap`、`/api/v1/artifacts` | 公开缓存与 runtime 索引 |
| `GET /api/v1/artifacts/{version}` | 该版本目录里的那个 runtime 产物（校验 size + SHA-256 后流式返回） |
| `GET /api/v1/playgrounds/{id}/commits/{commit}/manifest` | 该 commit 的文件清单 |
| `GET /api/v1/playgrounds/{id}/commits/{commit}/files/{path}` | 一个文件的确切字节 |
| `GET /courses/{id}/{commit}/{path}` | 课程页面；`libs/**`、`pyodide/**`、`pico.js` 等取自 runtime 目录 |

可读性只有一条规则：commit 必须在这个 Playground 的已发布历史里。这条检查在任何 tree 或 blob 读取之前执行。

## 写入

| 路径 | 谁 | 输入 |
| --- | --- | --- |
| `POST /api/v1/playgrounds` | operator token | `{"id": …}` |
| `POST /api/v1/playgrounds/{id}/commits` | operator token | ZIP + `Idempotency-Key` |
| `DELETE /api/v1/playgrounds/{id}` | operator token | — |
| `POST /api/v1/submissions` | 任何人 | `{playgroundId, commitId, student, files}` + `Idempotency-Key` |
| `GET /api/v1/submissions[/{id}]` | operator token | — |

- Operator token 在启动时给（`PICOPG_OPERATOR_TOKEN`），常量时间比较，不入库。没有它就拒绝启动：否则每个写端点都是公开的。
- Bundle 只允许 `course/**`、`starter/**`、`picopg.json`：拒绝绝对路径、`..`、链接、重复归一化路径、特殊文件、未知 schema/runtime 及超限内容（≤1024 文件、单文件 ≤1MiB、合计 ≤8MiB、ZIP ≤10MiB）。
- Submission 只允许 `work/**`，路径必须可移植（无保留名、无 `:`、无结尾点/空格、大小写不冲突），限额同上。提交的字节只进数据库，永远不进仓库。
- 下线（`DELETE`）只删那个 Playground 的 ref，顺带忘掉它的发布幂等记录：数据库行、commit 对象、已经收上来的作业都留着，用同一个 id 重新发布即恢复（恢复的是新的 commit，旧 commitId 不再可读）。ref 没了就没有“已发布历史”，所以目录、manifest、课程页一律看不到它。

## 一致性

- 数据库事务把业务行和幂等结果一起提交。
- Git 与数据库不能共用事务：先确认 Git object 存在，再 CAS 更新 ref，最后写引用。
- 缓存文件通过临时文件 + rename 原子替换，删掉可从权威重建。
- Git、`app.db`、`artifacts/` 是一个恢复集合；`generated/` 可弃。

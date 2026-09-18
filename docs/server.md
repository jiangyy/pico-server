# Server 契约

Server 只做两件事：分发已发布的课程内容，接收 client 的提交。它不执行学生代码，也不判断调用者是谁。

## 存储

```text
data/
  picopg.git/       私有裸 Git 仓库（只有 playgrounds ref）
  app.db            应用数据库（playgrounds、idempotency_results、submissions）
  artifacts/        不可变 runtime/EXE
  generated/        可重建的 catalog/manifest/bootstrap/index
  runtime/          课程页面按需加载的共享运行时
```

## 读取（公开）

| 路径 | 内容 |
| --- | --- |
| `GET /api/v1/catalog` | 已发布 Playground 的当前 commit，带分页 |
| `GET /api/v1/bootstrap` | 公开缓存（catalog + manifest index） |
| `GET /api/v1/playgrounds/{id}/commits/{commit}/manifest` | 该 commit 的文件清单 |
| `GET /api/v1/playgrounds/{id}/commits/{commit}/files/{path}` | 一个文件的确切字节 |
| `GET /courses/{id}/{commit}/{path}` | 课程页面；`libs/**`、`pyodide/**`、`pico.js` 等从 runtime 目录取 |
| `GET /api/v1/artifacts`、`GET /api/v1/artifacts/{version}/{name}` | 不可变 runtime 产物 |

这些路径全部匿名可用。可读性只有一条规则：commit 必须落在这个 Playground 的已发布历史里（`repository.commit_in_playground_history`）。这条检查在任何 tree 或 blob 读取之前执行。

## 写入

两种写入，各自只有一个授权来源。

| 路径 | 谁 | 输入 |
| --- | --- | --- |
| `POST /api/v1/playgrounds` | operator token | `{"id": …}` |
| `POST /api/v1/playgrounds/{id}/commits` | operator token | ZIP + `Idempotency-Key` |
| `POST /api/v1/submissions` | 任何人 | `{playgroundId, commitId, student, files}` + `Idempotency-Key` |
| `GET /api/v1/submissions`、`GET /api/v1/submissions/{id}` | operator token | — |

- Operator token 在启动时提供（`--operator-token` 或 `PICOPG_OPERATOR_TOKEN`），常量时间比较，不入库。没有 token 时 Server 拒绝启动：否则每个写端点都会变成公开的。
- Bundle 只允许 `course/**`、`starter/**` 和 `picopg.json`；拒绝绝对路径、`..`、链接、重复归一化路径、特殊文件、未知 schema/runtime 及超限内容。
- Submission 只允许 `work/**`；路径必须可移植（无保留名、无 `:`/结尾点或空格、大小写不冲突），entry 数、单文件字节和总字节都有上限。校验由 `works.validate_work_files` 一处负责。

## 一致性与运维

- 数据库事务原子写入业务对象及其幂等结果。
- Git 与数据库不能共用事务：先确保 Git object 存在，再 CAS 更新 ref；数据库引用只在成功后写。
- manifest、bootstrap 和 artifact index 通过临时文件原子替换，并可从权威数据重建。
- runtime/EXE 按版本不可变发布；Client 校验 size 与 SHA-256。
- Git、数据库和 artifact 作为一个恢复集合备份。Server 不记录 token，不做访问日志。

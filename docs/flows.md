# 端到端流程

对象含义见 [core.md](core.md)。

## 发布

1. `learn-python` 把 `course/**`、`starter/**`、`picopg.json` 打成 ZIP。
2. Operator 用 operator token 调 `POST /api/v1/playgrounds/{id}/commits`（ZIP + `Idempotency-Key`）；新课先 `POST /api/v1/playgrounds`。
3. Server 校验每个成员与限额，写 commit，CAS 更新 ref，重建缓存。
4. 缓存失败不回滚发布：权威在 Git 里，下次读取会重建。相同 tree 重复发布是 no-op，返回同一个 `commitId`。

## 安装、运行、过期

1. Client 读 `GET /api/v1/catalog` 拿 `(playgroundId, commitId)`，读 manifest 只下载本地缺的 blob。
2. 每个 blob 校验 size 与 SHA-256 后原子安装；manifest 在依赖齐全后原子替换，中断也不影响旧版本。
3. 课程页面按 `../` 引用共享 runtime（`../libs/…`、`../pyodide/…`、`../pico.css`），由 `/courses/**` 从 runtime 目录提供。
4. 编辑立即落本地草稿，Python 在本地跑，离线只用已装内容。
5. 本地内容是派生副本：删掉内容根就回到未安装，下次打开重下；草稿不在内容根里，清内容不丢草稿。

## 提交

1. Client 把草稿整理成有界的 `work/**` 文件集合，连同它基于的 `(playgroundId, commitId)` 和学生自称的姓名，`POST /api/v1/submissions`（带 `Idempotency-Key`）。
2. Server 校验字段、文件集合，确认该 commit 已发布，插入一条不可变记录。
3. 同一 key 的重试返回原记录；两名学生可以用同一个 key，因为幂等作用域是学生姓名。

Operator 用 `GET /api/v1/submissions` 分页看有哪些提交，用 `GET /api/v1/submissions/{id}` 取回文件内容。

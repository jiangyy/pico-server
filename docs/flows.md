# 端到端流程

本文只描述状态迁移；对象含义见 [core.md](core.md)。

## 发布

1. `learn-python` 构建 `course/**`、`starter/**` 和 `picopg.json`，打包成 ZIP。
2. Operator 用 operator token 调用 `POST /api/v1/playgrounds/{id}/commits`（ZIP + `Idempotency-Key`）；新 Playground 先 `POST /api/v1/playgrounds`。
3. Server 校验 bundle 的每个成员与限额，写 Git commit，并以 compare-and-swap 更新 Playground ref。
4. Server 重建公开缓存。缓存失败不回滚发布：权威已经在 Git 里，下一次读取会重建。

相同 tree 的重复发布是 no-op，返回同一个 `commitId`。

## 安装与运行

1. Client 读 `GET /api/v1/catalog` 拿到 `(playgroundId, commitId)` 列表。
2. Client 读该 commit 的 manifest，只下载本地缺的 blob，校验 size 与 SHA-256 后原子安装。
3. Client 打开课程页面；页面按 `../` 引用共享 runtime（`../libs/…`、`../pyodide/…`、`../pico.css`），这些由 `/courses/**` 从 runtime 目录提供。
4. 编辑立即落到本地草稿；Python 在本地跑。

离线时只使用已安装内容。刷新失败不得隐藏旧版本或丢失草稿。

## 过期与重下

本地安装内容是可以随时丢弃的派生副本：删掉内容根，课程就回到"未安装"，下次打开按需重下。草稿不在内容根里，所以清内容不会丢草稿。

## 提交

1. Client 把草稿整理成一个有界的 `work/**` 文件集合，连同它基于的 `(playgroundId, commitId)` 和学生自称的名字，`POST /api/v1/submissions`（带 `Idempotency-Key`）。
2. Server 校验字段与文件集合、确认该 commit 已发布，然后插入一条不可变记录。
3. 同一 key 的重试返回原来的记录；两名学生可以用同一个 key，因为幂等作用域是学生名字。

Operator 用 `GET /api/v1/submissions`（分页）看有哪些提交，用 `GET /api/v1/submissions/{id}` 取回完整文件内容。

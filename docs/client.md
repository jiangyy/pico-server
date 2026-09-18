# Client 契约

Client 只有 Windows 桌面（Electron），源码在 `learn-python` 仓库；`picopg` 只提供 API 与内容分发。

## 本地优先

- 先读本地内容再增量刷新；刷新或安装失败不影响已装版本。
- 已装内容是派生副本，删掉就回到未安装，下次打开重下。
- 草稿和 Python runtime 不在内容根里，清内容不丢草稿。

## 读取

- 目录来自 `GET /api/v1/catalog`，每条只有 `playgroundId` 与 `commitId`；未知字段必须忽略。
- 展示元数据（`title`、`summary`、`cover`）在该 commit 的 `picopg.json` 里，不来自 catalog。
- 每个 blob 校验 size 与 SHA-256；manifest 原子替换。
- Python runtime 随 Client 分发，不属于课程内容。

## 编辑与提交

- 编辑立即落本地草稿，按 `playgroundId` 隔离，记录派生自的 `playgroundCommitId`。
- 提交是 `POST /api/v1/submissions`：`{playgroundId, commitId, student, files}`，`files` 是 `work/**` 路径到文本，必须带 `Idempotency-Key`（由请求内容派生，重试自动复用）。
- `student` 是自报显示名，Server 不校验也不据此授权；Client 必须在提交前让用户确认。
- 提交成功不改变本地草稿：Server 没有"已交"状态，重交只是多一条记录。
- 失败逐项显示；重试沿用同一个 key。

## 边界

- 课程跑在无 Node 权限的 renderer 里，严格 CSP。
- Bridge 只暴露草稿读写和提交；没有通用命令通道。
- Operator token 只存在于 Server 运维侧，Client 从不持有。

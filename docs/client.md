# Client 契约

Client 只有一个平台：Windows 桌面（Electron）。它属于 `learn-python` 仓库；`picopg` 只拥有 Server API 与内容分发，不接收也不托管 Client 产品壳。

## 本地优先

- Client 先把课程内容留在本地，再按需刷新网络数据。刷新或安装失败不影响已安装版本。
- 课程内容是可以随时丢弃的派生副本：删掉内容根，课程回到"未安装"，下次打开按需重下。
- 草稿、运行环境不在内容根里，清内容不会连草稿一起删。

## 读取

- 目录来自 `GET /api/v1/catalog`，每条只有 `playgroundId` 与 `commitId`；未知字段必须忽略（`releaseIds` 之类的旧字段不再出现）。
- 展示元数据（`title`、`summary`、`cover`）在该 commit 的 `picopg.json` 里，不来自 catalog 投影。
- 下载的 blob 通过 size 与 SHA-256 校验；manifest 在依赖完整后原子替换，保证中断后旧版本仍可打开。
- Python runtime 随 Client 分发，不属于课程内容，所以清理内容不影响它。

## 编辑与提交

- 编辑立即落入本地草稿，草稿按 `playgroundId` 隔离，记录它派生自的 `playgroundCommitId`。
- 提交是 `POST /api/v1/submissions`：`{playgroundId, commitId, student, files}`，`files` 是 `work/**` 路径到文本的映射，必须带 `Idempotency-Key`。
- `student` 是自报的显示名，Server 不校验也不据此授权；Client 必须让用户在提交前确认它是谁。
- 提交成功后本地草稿保留：Server 不返回"已交"状态，重交只会多一条记录。
- 失败逐项显示；重试沿用同一个 `Idempotency-Key`。

## 安全边界

- 课程运行在无 Node.js/系统权限的 renderer 中，并使用严格 CSP。
- Bridge 只暴露加载/保存草稿、提交与受控事件；没有通用命令通道。
- Operator token 只存在于 Server 运维侧。Client 从不持有它，也从不调用 operator 端点。

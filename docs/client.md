# Client 契约

Client 只有一个平台：Windows 桌面（Electron）。平台能力集中在 host adapter：桌面使用文件缓存与隔离 bridge。

## 产品入口与产物

`learn-python` 拥有 Client UI 源码及桌面构建；`picopg` 只拥有 Server API 与远程资源分发，不接收或托管 Client 产品壳。官网 Portal 是独立静态产物，不是 Client。

- Student 视图是桌面 Client 的真实 consumer。
- Teacher 和 Admin 没有 Client。两个角色仍存在于 Server（授权、发布 Release、点评都归它），由运维通过 Server API 使用；客户端不实现这两个视图，也不做角色路由。
- Client 不从 URL 或本地标记推导授权，只接受 Server 返回的 `role`：非 `student` 会话在进入界面之前就被拒绝。
- Playground `course/index.html` 是发布器从课程源码生成的不可变内容 artifact，不是产品壳入口。

单文件 Student/Teacher/Admin HTML 是迁移期的设计参考，随 Web 产物一起归档，不是发布 contract。

## Local-first

Client 启动时先读取本地 manifest、内容、Draft 和 runtime，再增量刷新网络数据。失败的刷新或安装不影响已安装版本。

Draft 按用户和 Playground 隔离，记录 Playground commit、base Work commit、文件、dirty 状态和保存时间。本地保存失败必须显式展示；缓存清理不得删除 manifest 或 Draft 引用的 blob。

下载的 blob 通过 size 与 SHA-256 校验。manifest 在依赖完整后原子替换，保证中断后旧版本仍可打开。Python runtime 随 Client 一起分发，不属于课程内容（`mount.ts` 的 `runtimeDir`），因此内容的清理与安装都不影响它。

内容是可以随时丢弃的派生副本：删除本地内容根只会让课程重新回到“未安装”，打开时按需重新下载；Draft、Work HEAD、提交收据与幂等键不在内容根内，不随内容一起消失。

## 编辑、同步与提交

- 编辑立即落入本地 Draft；Python 在隔离 renderer 的 Pyodide 中运行。
- Sync 发送有界的完整 `work/**` 快照。成功后先更新本地 HEAD，再按 Draft 版本决定是否标为 clean。
- `409 work_head_changed` 保留 Draft，并让用户选择 reload、显式 overwrite 或本地 merge。
- Submit 先同步脏 Draft，再创建固定 Work commit 的 Submission；每一步单独显示失败，重试沿用幂等键。
- 重连后刷新 Release、Submission 和 Comment；本地数据按稳定 ID 去重。

## 安全边界

课程运行在无 Node.js/系统权限的 renderer 中，并使用严格 CSP。Bridge 仅暴露加载/保存 Draft、提交与受控事件；两端校验消息，不提供通用命令通道。

Session token 由 host 的凭据设施保存。token 与认证响应不得进入不可信课程页面。

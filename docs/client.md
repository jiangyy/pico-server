# Client 契约

Web 与桌面端共享路由、组件、状态与 API 类型。平台能力集中在 host adapter：Web 使用浏览器存储和 Pyodide，桌面使用文件缓存与隔离 bridge。

## Local-first

Client 启动时先读取本地 manifest、内容、Draft 和 runtime，再增量刷新网络数据。失败的刷新或安装不影响已安装版本。

Draft 按用户和 Playground 隔离，记录 Playground commit、base Work commit、文件、dirty 状态和保存时间。本地保存失败必须显式展示；缓存清理不得删除 manifest 或 Draft 引用的 blob。

下载的 blob/runtime 通过 size 与 SHA-256 校验。manifest 在依赖完整后原子替换，保证中断后旧版本仍可打开。

## 编辑、同步与提交

- 编辑立即落入本地 Draft；Python 在隔离 renderer 的 Pyodide 中运行。
- Sync 发送有界的完整 `work/**` 快照。成功后先更新本地 HEAD，再按 Draft 版本决定是否标为 clean。
- `409 work_head_changed` 保留 Draft，并让用户选择 reload、显式 overwrite 或本地 merge。
- Submit 先同步脏 Draft，再创建固定 Work commit 的 Submission；每一步单独显示失败，重试沿用幂等键。
- 重连后刷新 Release、Submission 和 Comment；本地数据按稳定 ID 去重。

## 安全边界

课程运行在无 Node.js/系统权限的 renderer 中，并使用严格 CSP。Bridge 仅暴露加载/保存 Draft、提交与受控事件；两端校验消息，不提供通用命令通道。

Session token 由 host 的凭据设施保存。token 与认证响应不得进入不可信课程页面。

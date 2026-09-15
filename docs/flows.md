# 端到端流程

本文只描述状态迁移；对象含义见 [core.md](core.md)。

## 发布

1. `learn-python` 构建 `course/**`、`starter/**` 和 `picopg.json`，以幂等请求上传 bundle。
2. Server 校验内容与限制，写 Git commit，并以 compare-and-swap 更新 Playground ref。
3. Server 重建 manifest 和公开 bootstrap 缓存。

发布不授予访问权。新 Playground 默认 `release_only`；相同 tree 的重复发布是 no-op。

## 安装与运行

1. Client 先展示本地内容，再合并公开目录与当前 Class 可见的 Release。
2. Client 按 commit manifest 下载缺失 blob/runtime，校验 size 与 SHA-256 后原子安装。
3. Client 在隔离 renderer 中打开课程；Pyodide 与执行输出留在本地。
4. 每次编辑立即保存到当前用户的本地 Draft。

离线时只使用已安装内容。刷新或安装失败不得隐藏旧版本或丢失 Draft。

## 同步与恢复

1. Client 上传完整且有界的 `work/**` 快照、`playgroundCommitId` 与 `baseCommitId`。
2. Server 校验所有权、内容授权和当前 Work HEAD，创建 commit 后以 compare-and-swap 更新 ref。
3. Client 记录返回的 commit ID；仅当 Draft 未在请求期间变化时将其标为 clean。

HEAD 不匹配时返回冲突和最新 HEAD。Client 保留 Draft，由用户 reload、基于最新 HEAD 再次 overwrite，或在本地 merge。

恢复历史时，Server 以历史 tree 为内容、当前 HEAD 为 parent 创建新 commit。

## 发布作业与审阅

1. Teacher 为自己拥有的 Class 创建固定 Playground commit 的 Release；学生仅在 `openAt` 后且仍是当前成员时可见。
2. Client 提交前先同步脏 Draft，再以同一 Playground 版本的 Work commit 创建 Submission；Server 同时校验当前 ClassMembership。
3. Teacher 读取该快照并添加 Comment；授权的 AI/system 也使用 Comment，且显式记录 source。

Release、Submission 和 Comment 均为不可变记录。`requiresSubmission` 为真时，`dueAt` 后拒绝新 Submission。Client 重连后重新读取当前 canonical 数据，不依赖实时通知。

转班后，学生按新 Class 获取 Release；已经创建的 Submission 与 Comment 保持不变。

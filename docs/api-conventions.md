# HTTP 约定

- Base path 为 `/api/v1`；JSON 使用 UTF-8、camelCase。
- 时间戳使用 UTC RFC 3339；ID 与 commit ID 均为不透明字符串。
- Client 必须忽略未知响应字段。
- 认证使用 `Authorization: Bearer <sessionToken>`；token 只显示一次，Server 只存哈希。
- 错误返回稳定的机器可读 `code` 与可选 `details`，不得要求 Client 解析 message。

## 写入

创建 Playground commit、Release、Submission 或 Comment 时必须携带 `Idempotency-Key`。同一 actor、端点、key 和 body 返回原结果；body 不同则返回 `409`。

Git ref 写入使用 compare-and-swap。Work sync 还携带 `baseCommitId`；HEAD 不匹配返回 `409 work_head_changed` 和 `currentHeadCommitId`。

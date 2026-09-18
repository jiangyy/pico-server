# HTTP 约定

- Base path 为 `/api/v1`；JSON 使用 UTF-8、camelCase。
- 时间戳使用 UTC RFC 3339；ID 与 commit ID 均为不透明字符串。
- Client 必须忽略未知响应字段。
- 读取不需要任何凭据。
- 写 Playground bundle 需要 `Authorization: Bearer <operatorToken>`。token 由运维在服务端配置，不通过 HTTP 发放，也不入库。
- 错误返回稳定的机器可读 `code` 与可选 `details`，不得要求 Client 解析 message。

## 写入

创建 Playground commit 或 Submission 时必须携带 `Idempotency-Key`：同一作用域、端点、key 与 body 返回原结果；body 不同返回 `409`。

- Playground 发布的幂等作用域是 operator。
- Submission 的幂等作用域是学生自报的名字，所以两名学生可以用同一个 key 而不互相干扰。

Git ref 写入使用 compare-and-swap：并发的发布只有一次成功，失败方收到 `409 playground_head_changed`。

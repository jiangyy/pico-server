# HTTP 约定

- Base path `/api/v1`；JSON 用 UTF-8、camelCase。
- 时间戳 UTC RFC 3339；ID 与 commit ID 是不透明字符串。
- Client 必须忽略未知响应字段。
- 读不需要凭据；写 Playground 需要 `Authorization: Bearer <operatorToken>`，token 由运维在服务端配置，不通过 HTTP 发放。
- 错误返回稳定的 `code` 与可选 `details`；Client 不得解析 message。

## 写入

`POST /api/v1/playgrounds/{id}/commits` 和 `POST /api/v1/submissions` 必须带 `Idempotency-Key`：同一作用域、端点、key 与 body 返回原结果；body 不同返回 `409`。

- 发布的幂等作用域是 operator。
- 提交的幂等作用域是学生自报的名字，所以两名学生可以复用同一个 key。

Git ref 写入用 compare-and-swap：并发发布只有一次成功，失败方收到 `409 playground_head_changed`。

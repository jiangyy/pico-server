# PicoPG Server 机制

PicoPG Server 负责保存可复制的事实、接收允许由 Client 写入的事实、发布由 Server 拥有的事实，并在事实发生变化后通知在线 Client。

Server 还负责 Playground 包的接收、校验、持久化、版本发布和下载。

Server 不负责解释所有业务。

## Server 内部的分层

Server 目前有四个彼此分开的部分。

第一部分是 Replica Log 契约。它只定义查询 head、判断一个位置是否存在、分页读取和条件追加，不知道 HTTP、Playground 或 ZIP。

第二部分是文件持久化实现。它把 Log 保存到磁盘，负责校验父子链、Record 身份和跨 Log 依赖，并保证成功响应之前数据已经完成持久化。

第三部分是远程协议层。它把 Replica Log 操作提供给 Client，负责握手、认证、Log 权限和 SSE 通知。

第四部分是 Playground 发布层。它负责包格式校验、包文件存储和发布事实生成，但不改变 Replica Log 的通用语义。

当前 ZIP v1 adapter 要求 `picopg.json` 声明 `schemaVersion: 1`、`runtime: "python"` 和包内 `module` 路径；`course/index.html` 与该 JavaScript 模块都必须真实存在。HTTP 发布层只依赖 `PackageAdapter` 返回的检查结果，未来替换包格式不需要修改 Log、权限或发布事实。

Principal 会话可以声明 `playgrounds`、`drafts`、`submissions` 语义能力。授权层根据 Principal 与 Replica 身份派生实际 Log 权限：设备只能追加自己的 Revision、Draft Request 和 Submission Request，同一 Principal 的其他设备只能读取 Revision，Server 拥有的 Decision、Comment 和 Submission Result 只读。动态 Draft Request Log 会被 worker 挂载；重启时从持久存储枚举并恢复，避免配置不再逐 Log 列举后漏处理请求。

# PicoPG Server

PicoPG Server 保存和复制不可变事实，并处理只能由 Server 决定的结果。它不解释源码语义，也不负责 Client 的 Workspace 或界面状态。

## 已经实现

- 文件型 Replica Log 持久化、记录校验、条件追加和重启恢复。
- HTTP 握手、head/has/read/append、分页边界、请求体限制与 SSE 通知。
- `Authenticator`、`AuthenticatedReplica`、`ReplicaDirectory` 与 `LogAuthorization` 分层；静态 token 配置只存在于 composition root。
- 日志使用 `principal/{principalId}/...` 命名空间；设备只能追加自己的 Log，同一用户的设备可以互读 Revision，不同用户即使 Draft ID 相同也完全隔离。
- Draft begin/advance/takeover 判定、epoch fencing、拒绝结果持久化与幂等重放。
- Submission Request 消费、精确 Revision 读取、Git tag 写入和 Result Log 回传。
- Playground ZIP v1 校验、内容寻址存储、发布冲突检查、Catalog 和精确版本下载。
- Server-owned Comment Log 与 operator 写入接口。
- worker 启动时枚举已有 Request Log，运行时动态发现 Draft 与 Submission Request Log，不依赖认证配置推导任务。
- protocol、application、worker、comment 测试，以及真实 Client/Server 端到端测试。

## 还没有完全完成

- 正式 Auth：当前 token 和 Principal 来自静态配置；没有登录、Session 刷新/撤销、Replica 注册、课程/班级授权或角色管理。
- 生产存储：Replica Log、Package 仍使用本机文件；没有 PostgreSQL adapter、Object Storage、migration 和完整备份恢复流程。
- 部署能力：当前适合单实例；缺少 TLS/反向代理配置、Secret 管理、结构化监控、告警、速率限制和生产 runbook。
- 横向扩容：worker 与 HTTP Server 同进程；多实例前需要共享 Store、worker lease/竞争控制和唯一结果写入保障。
- 数据管理：Git repository 映射仍是静态配置；没有 Submission 查询索引、历史管理、保留策略和自动评测编排。
- Comment 模型：Comment 当前属于 Draft，尚未绑定某次 Revision 或 Submission。
- 部署后验证：已有本地 HTTP 测试，但还没有真实 HTTPS、代理、断网、重启、备份恢复和容量测试。

## 下一步

1. 用正式登录/Session 实现替换静态 `Authenticator`，继续输出相同的 `AuthenticatedReplica`，不修改 Draft 协议。
2. 明确每类数据的唯一权威，再接 PostgreSQL、Object Storage 和 Git 管理；避免同一事实多处双写。
3. 先完成可恢复的单实例部署，再做 staging HTTP、鉴权、重启和故障测试。
4. 只有真实负载证明需要时，才拆 worker、共享 Store 和横向扩容。

无论底层存储和部署方式怎样变化，都必须保持：单写者、精确 Revision 引用、结果可重放、通知不承载事实、Server 不合并源码。

# PicoPG Server

PicoPG Server 保存和复制不可变事实，并处理只能由 Server 决定的结果。它不解释源码语义，也不负责 Client 的 Workspace 或界面状态。

## 已经实现

- 文件型 Replica Log 持久化、记录校验、条件追加和重启恢复。
- HTTP 握手、认证身份读取、head/has/read/append、分页边界、请求体限制与 SSE 通知。
- `Authenticator`、`AuthenticatedReplica`、`ReplicaDirectory` 与 `LogAuthorization` 分层；静态 token 配置只存在于 composition root。
- 日志使用 `principal/{principalId}/...` 命名空间；设备只能追加自己的 Log，同一用户的设备可以互读 Revision，不同用户即使 Draft ID 相同也完全隔离。
- Draft begin/advance/takeover 判定、epoch fencing、拒绝结果持久化与幂等重放。
- Submission Request 消费、精确 Revision 读取、Git tag 写入和 Result Log 回传。
- Playground ZIP v1 校验、内容寻址存储、发布冲突检查、Catalog 和精确版本下载。
- Server-owned Comment Log 与 operator 写入接口。
- 持久 Browser Session、Device handle 与 Principal-local Replica 注册；退出不删除历史 Replica 归属。
- 独立 operator Session，以及从 Submission/Revision Log 派生的提交列表、精确源码详情和 targeted Comment API。
- worker 启动时枚举已有 Request Log，运行时动态发现 Draft 与 Submission Request Log，不依赖认证配置推导任务。
- nginx HTTPS/SSE 配置、systemd 单实例配置、原子备份/空目录恢复工具和部署 smoke。
- protocol、application、worker、comment、session、submission、backup 与 smoke 测试，以及真实 Client/Server 端到端测试。

## 还没有完全完成

- 完整账号系统：当前是受控发放的 access code 和固定 30 天 Browser Session；没有密码找回、OAuth、教师角色或凭据分发系统。Profile、班级、选课明确不属于认证模型。
- 生产存储：Replica Log、Package 与 Session registry 仍使用本机文件；已有一致性优先的停机备份恢复，但没有 PostgreSQL adapter、Object Storage、在线备份或 migration framework。
- 横向扩容：worker 与 HTTP Server 同进程；多实例前需要共享 Store、worker lease/竞争控制和唯一结果写入保障。
- 数据管理：Git repository 映射仍是静态配置；Submission 查询当前直接扫描 Log，没有历史筛选、保留策略和自动评测编排。
- 部署后验证：仓库内已有 HTTP、Session、SSE 与备份恢复 smoke；真实证书、nginx、systemd restart、离机恢复和容量测试必须在 Linux staging 执行。

## 下一步

1. 在 staging 按 `deploy/README.md` 验证 HTTPS、SSE、鉴权、systemd 重启和离机备份恢复。
2. 用少量真实用户验证 access code 分发、离线恢复、分叉选择和教师评语流程。
3. 课堂试用确认后，在独立业务层设计 Profile、Class、Enrollment 与 Assignment；不改变 Log identity。
4. 只有真实负载证明需要时，才引入 PostgreSQL、Object Storage、查询索引、拆分 worker 或横向扩容。

无论底层存储和部署方式怎样变化，都必须保持：单写者、精确 Revision 引用、结果可重放、通知不承载事实、Server 不合并源码。

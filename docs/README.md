# PicoPG 设计

PicoPG 是 local-first 的 Python 学习系统：`learn-python` 发布课程，Client 本地安装、编辑和运行，Server 负责同步、历史与教学协作。

按需阅读：

1. [core.md](core.md)：领域模型与不变量（权威定义）
2. [flows.md](flows.md)：端到端状态迁移
3. [api-conventions.md](api-conventions.md)：HTTP 约定
4. [server.md](server.md)、[client.md](client.md)、[account.md](account.md)：组件边界
5. [client-display.md](client-display.md)：客户端展示契约的 Server 待办清单

冲突时，以 `core.md`、相关组件契约、实现注释的顺序为准。

## 非目标

- Client 不操作 Git；Git 不保存账号、权限和教学流程。
- Server 不运行学生代码，也不自动合并学生编辑。
- v0.1 不拆分多个 Git 仓库，不实现课程升级或实时推送。

## 实施计划

1. 建立数据库、认证、Class、Playground 目录和内容发布。
2. 完成公开/授权内容下载、manifest 与 runtime 分发。
3. 完成 Client 离线安装、Draft、Pyodide 与隔离边界。
4. 完成 Work 初始化、CAS 同步和追加式恢复。
5. 完成按 Class 发放的 Release、Submission、Comment 与角色授权。
6. 补齐备份、缓存重建、审计和端到端故障测试。

# PicoPG 设计

PicoPG 是 local-first 的 Python 学习系统：`learn-python` 发布课程，Client 把课程装在本地、按需拉取资源、随时可以把内容删掉重下，并把学生交的作业送回来。

Server 只有两件事：

1. **分发内容**：接收 operator 上传的课程 bundle，向任何 client 提供 catalog、manifest、文件字节和共享 runtime。
2. **接收提交**：接收 client 交上来的有界文件集合，并把它作为不可变记录存下来。

按需阅读：

1. [core.md](core.md)：领域模型与不变量（权威定义）
2. [flows.md](flows.md)：端到端状态迁移
3. [api-conventions.md](api-conventions.md)：HTTP 约定
4. [server.md](server.md)、[client.md](client.md)：两侧的边界

冲突时，以 `core.md`、相关组件契约、实现注释的顺序为准。

## 非目标

- 没有账号、角色、班级、发布范围、点评：Server 不判断"这个人是谁"。
- Client 不操作 Git；Git 不保存提交记录。
- Server 不运行学生代码，不合并学生编辑。
- 读取一律公开；写只有两种：operator 的 bundle、client 的提交。

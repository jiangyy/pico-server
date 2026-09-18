# PicoPG 设计

`learn-python` 发布课程，Client 把课程装在本地、按需拉取、随时丢弃重下，并把学生交的作业送回来。

Server 只有两件事：**分发已发布的课程内容**，**接收 client 的提交**。

1. [core.md](core.md)：对象与不变量（权威定义）
2. [flows.md](flows.md)：发布、安装、提交
3. [api-conventions.md](api-conventions.md)：HTTP 约定
4. [server.md](server.md)、[client.md](client.md)：两侧边界
5. [deployment.md](deployment.md)：picopg.cn 运维

没做的：账号、会话、班级、发布范围、点评、实时推送。读取一律公开；写只有 operator 的 bundle 和 client 的提交。

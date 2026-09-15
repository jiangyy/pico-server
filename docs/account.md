# 账号与授权

- `Student` 拥有 Work 并创建 Submission。
- `Teacher` 向关联学生发布 Release，并审阅对应 Submission。
- `Admin` 管理账号、师生关系和 Playground 策略，默认不读学生源码。

公开注册只创建 Student；Teacher/Admin 由 Admin 授权。密码使用独立 salt 的 memory-hard 哈希。

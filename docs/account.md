# 账号与授权

- `Student` 属于至多一个当前 Class，拥有 Work 并创建 Submission。
- `Teacher` 拥有 Class，向班级发布 Release，并审阅对应 Submission。
- `Admin` 管理账号、Class、ClassMembership 和 Playground 策略，默认不读学生源码。

公开注册只创建 Student；Teacher/Admin 由 Admin 授权。密码使用独立 salt 的 memory-hard 哈希。

师生关系由 Class 与当前 ClassMembership 推导，不另存第二份名单。转班不修改既有 Submission 与 Comment。

# Server 契约

Server 负责认证授权、导入课程、执行 Work Git 操作、存储教学流程，并分发内容与 runtime。它不执行学生代码，不把缓存当作权威。

## 存储

```text
data/
  picopg.git/       私有裸 Git 仓库
  app.db            应用数据库
  artifacts/        不可变 runtime/EXE
  generated/        可重建的 manifest/bootstrap/index
  runtime/          课程页面按需加载的共享运行时
```

v0.1 使用一个仓库和一个数据库；只有出现经测量的容量或锁问题时才拆分。

## 写入边界

发布 bundle 仅允许普通文件 `course/**`、`starter/**` 和 `picopg.json`。Server 拒绝绝对路径、`..`、链接、重复归一化路径、特殊文件、未知 schema/runtime 及超限内容。

Work sync 仅接受有界的 `work/**` 快照。Server 自行组装完整 tree，并校验所有权、内容访问权、Playground 版本、文件限制和 `baseCommitId`。仓库不暴露原生 Git 传输。

## 读取授权

内容 commit 只有在下列情况之一可读：公开且未归档；学生的当前 Class 拥有指向它的开放 Release，或学生自己的 Work 引用它；教师拥有引用它的 Release 所属 Class；调用者是 Admin。知道 commit ID 不构成授权。

教师只能读取自己 Class 的 Release 下的 Submission。Admin 管理账号、Class 与策略，但默认没有学生源码读取权。

## 一致性与运维

- 数据库事务原子写入业务对象及其幂等结果。
- Git 与数据库不能共用事务：先确保 Git object 存在，再写数据库引用；Git ref 更新必须可重试。
- manifest、bootstrap 和 artifact index 通过临时文件原子替换，并可从权威数据重建。
- runtime/EXE 按版本不可变发布；Client 校验 size 与 SHA-256。
- Git、数据库和 artifact 作为一个恢复集合备份；日志记录 actor、目标、结果和 request ID，不记录 token 或源码。

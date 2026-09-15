# 核心模型

## 对象

| 对象 | 存储 | 含义 |
| --- | --- | --- |
| `Playground` | Git | 发布者创建的不可变课程版本 |
| `Work` | Git | 学生对一个 Playground 的线性编辑历史 |
| `Class` | 数据库 | 一位教师拥有的教学范围 |
| `ClassMembership` | 数据库 | 学生当前所属的一个 Class |
| `Release` | 数据库 | 教师向一个 Class 开放的确切 Playground commit |
| `Submission` | 数据库 | 一次提交，固定 Release 与 Work commit |
| `Comment` | 数据库 | 附属于 Submission 的不可变反馈 |

账号、会话和访问策略是普通数据库数据。师生关系由 ClassMembership 推导，不另存一份名单。

## Git

Server 独占一个私有裸仓库：

```text
refs/heads/playgrounds/<playgroundId>
refs/heads/works/<playgroundId>/<studentId>
```

Playground tree 只含 `course/**`、`starter/**` 和 `picopg.json`。Work tree 在此基础上增加学生拥有的 `work/**`；Server 写入 `.picopg/work.json`，记录固定的 `playgroundCommitId`。

首个 Work commit 的 parent 是其 Playground commit；后续 commit 以当前 Work HEAD 为 parent。发布与同步都以 compare-and-swap 更新 ref。恢复历史也创建新 commit，不回退 ref。

v0.1 不升级 Work 的 Playground 版本，也不产生 merge commit。

## 权威来源

| 事实 | 权威来源 |
| --- | --- |
| Playground、Work 的内容与历史 | Git object/ref |
| 账号、策略、Class、Release、Submission、Comment | 数据库 |
| manifest、bootstrap、索引 | 可重建缓存 |
| 安装内容、Draft、运行环境 | Client 本地存储或不可变 artifact |

## 不变量

1. API 中的 ID（包括 commit ID）都是不透明字符串。
2. Release 与 Submission 固定不可变 commit；更正会创建新记录。
3. 学生只能写自己的 `work/**`；发布者只能写 Playground tree；`.picopg/**` 只由 Server 写。
4. Sync 仅在 `baseCommitId` 等于 Work HEAD 时成功；冲突不丢 Draft、不自动合并。
5. commit ID 不代表授权；`release_only` 内容始终检查身份与当前 ClassMembership。
6. 教师只读自己 Class 的 Release 下的 Submission；Admin 默认不读学生 Work。
7. 转班改变后续可见的 Release，不修改既有 Submission 与 Comment。
8. Git 只保存源码快照，数据库只保存业务状态。

# 核心模型

## 对象

| 对象 | 存储 | 含义 |
| --- | --- | --- |
| `Playground` | Git + 一行数据库记录 | 一个不透明的课程 id，加它的提交历史 |
| `Submission` | 数据库 | 一次提交：学生交上来的有界文件集合，固定一个已发布的 Playground commit |

## Git

Server 独占一个私有裸仓库，只有一类 ref：

```text
refs/heads/playgrounds/<playgroundId>
```

一个 Playground tree 只含 `course/**`、`starter/**` 和 `picopg.json`。发布用 compare-and-swap 更新 ref：tree 不变时是 no-op，tree 变了就追加一个 commit，历史永不重写。

提交不进 Git。学生交上来的是一个 JSON 文件集合，它记录在数据库里；Server 从不把学生内容写进仓库，所以一次提交无法改变别的 client 下载到的东西。

## 权威来源

| 事实 | 权威来源 |
| --- | --- |
| Playground 内容与历史 | Git object/ref |
| Playground 是否存在 | 数据库的一行 |
| Submission | 数据库 |
| manifest、bootstrap、artifact index | 可重建缓存 |
| 已安装内容、草稿、运行环境 | Client 本地存储或不可变 artifact |

## 不变量

1. API 中的 ID（包括 commit ID）都是不透明字符串。
2. 只有 operator token 能写 Playground tree；请求体里的任何值都不能决定 ref、parent 或作者身份。
3. 一个 commit 只有落在它那个 Playground 的已发布历史里才可读；知道 commit ID 本身不构成可读性。
4. 读取一律公开：没有会话、没有角色、没有请求级判断。
5. Submission 只记录一个已发布的 `(playgroundId, commitId)`；它不复制课程内容。
6. Submission 只追加：没有更新、删除、状态。重交是第二条记录。
7. 同一 `Idempotency-Key` 与同一 body 返回原结果；body 不同返回 `409`。
8. Git 只保存课程快照，数据库只保存业务状态。

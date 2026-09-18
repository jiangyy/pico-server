# 核心模型

| 对象 | 存储 | 含义 |
| --- | --- | --- |
| `Playground` | Git + 一行记录 | 一个不透明课程 id 和它的提交历史 |
| `Submission` | 数据库 | 一次提交：学生交的文件集合，固定一个已发布的 commit |

## Git

一个私有裸仓库，只有一类 ref：

```text
refs/heads/playgrounds/<playgroundId>
```

Playground tree 只含 `course/**`、`starter/**`、`picopg.json`。发布用 compare-and-swap：tree 不变是 no-op，变了就追加 commit，历史永不重写。

提交不进 Git。学生内容只进数据库，所以一次提交改变不了别的 client 下载到的东西。

## 权威来源

| 事实 | 来源 |
| --- | --- |
| 课程内容与历史 | Git object/ref |
| Playground 是否存在、Submission | 数据库 |
| manifest、bootstrap、artifact index | 可重建缓存 |
| 已装内容、草稿、运行环境 | Client 本地 |

## 不变量

1. API 里的 ID（含 commit ID）都是不透明字符串。
2. 只有 operator token 能写 Playground tree；请求体决定不了 ref、parent 或作者。
3. 一个 commit 只有落在它那个 Playground 的已发布历史里才可读；知道 commit ID 不等于可读。
4. 读取公开：没有会话、没有角色、没有请求级判断。
5. Submission 只记录一个已发布的 `(playgroundId, commitId)`，不复制课程内容，只追加，不更新不删除。
6. 同一 `Idempotency-Key` 与同一 body 返回原结果；body 不同返回 `409`。

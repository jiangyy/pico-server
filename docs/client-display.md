# 客户端展示契约：Server 侧待办（草案 v0.1）

状态：**待 server 排期确认**。来源：`learn-python` 的 `docs/course-presentation.md`
（课程名/摘要/封面/专属玩具）与 `docs/user-names.md`（人名），以及 owner 规则：

> 不透明 id 只用于管理与协议层；用户界面上看到的一律是名称。

本文只列**尚未由 server 实现**的部分，并注明已完成的 S1/S2/S3 与 Work 读取 API，避免重复。
字段一律可选、旧客户端忽略、新客户端对缺失回退；不提升 `schemaVersion`。

## 0. 已完成（不要重复实现）

- S1：客户端读该 commit 的 `picopg.json` 可选 `title`/`summary`（server 忽略未知字段，无需改动即可用）。
- S2：`GET /api/v1/classes/{classId}/members` 带 `username`；`GET /api/v1/releases/{releaseId}/submissions` 带 `username`。
- S3：`GET /api/v1/submissions`（仅返回调用者自己的）。
- Work 读取 API（学生读自己的 Work、教师读班级内提交的 Work）。

## 1. catalog 投影展示元数据（推荐）

**现状**：客户端为每门课额外 `GET .../files/picopg.json` 一次（Web 端每卡一次请求；桌面端读已安装文件）。
**请求**：`GET /api/v1/catalog`（与 `generated/catalog.json`、`bootstrap.json`）的每个条目增加可选字段：

```json
{
  "playgroundId": "hanoi",
  "commitId": "…",
  "releaseIds": ["…"],
  "title": "汉诺塔",
  "summary": "拖动最上面的圆盘，把所有圆盘搬到另一根柱子上。",
  "toy": { "entry": "toy/index.html", "thumbnail": "toy/card.svg", "interactive": true }
}
```

- 元数据来自**该条目的那个 commit** 的 `picopg.json`（Release 固定旧 commit 时展示旧元数据）。
- 旧 commit 缺字段 → **省略字段**（不是空串、不是 id）。
- 缓存仍是可重建投影：删除 `generated/` 后 rebuild 结果一致。
- 同一份 catalog 也通过 `GET /api/v1/bootstrap` 提供（`7d91281` 已实现，带 ETag/304）；
  两处必须同时带上新增元数据。

## 1b. 教师可见的「可发布内容」列表（阻塞教师发 Release）

**现状**：`POST /api/v1/classes/{classId}/releases` 允许任何教师发布任何未归档 Playground
（`_authorize_playground` 只查存在 + 未归档，不要求 public），但客户端没有列表来源，
教师端「资源池」恒为空（`resources: []`）——**教师在界面上发现不了新内容，Release 写入没有入口**。

**请求**：`GET /api/v1/playgrounds`（Teacher/Admin）→ 未归档 Playground 列表：

```json
{"playgrounds": [
  {"id": "hanoi", "commitId": "…", "title": "汉诺塔", "summary": "…", "requiresSubmission": true}
]}
```

- `commitId` 是当前 ref 的 commit（教师发布时固定它；旧版本需要 id 的话可另议）。
- `title`/`summary` 来自该 commit 的 `picopg.json`（与 §1 同一投影）。
- `requiresSubmission` 目前没有来源，二选一（请确认）：`picopg.json` 增加声明（例 `"kind": "assignment" | "exercise"`），
  或客户端在发布抽屉里让教师选（默认作业）。
- 授权：Teacher/Admin 可读。这是一个「可发布内容列表」，不改变内容读取授权——教师仍必须拥有班级
  才能创建 Release，学生仍只能通过 Release/public 看到内容。
- 排序稳定；已归档默认不返回。

## 2. `toy/**` bundle 前缀与玩具声明

- `bundles.ALLOWED_PREFIXES` 增加 `"toy/"`（`.picopg/`、`work/` 仍禁止）。
- `picopg.json` 可选字段校验（形状错 → `400 invalid_bundle` + `details.reason`，与现有 bundle 校验的错误码一致）：
  - `toy`：对象；`toy.interactive`：bool（必填）；
  - `toy.entry`：string，`toy/` 下、`.html` 结尾、必须在本次 bundle 内；
  - `toy.thumbnail`：可选 string，`toy/` 下、必须在 bundle 内、扩展名 `svg/png/webp/jpg`。
- 额外限额（保护首页卡片）：`toy/**` 总计 ≤ 1 MiB、≤ 64 个文件、`toy/index.html` ≤ 256 KiB。
- 背景：卡片会用 iframe 加载 `toy.entry`、用 `toy.thumbnail` 当封面；两者都走现有
  `files/{path}` 下载与本地 store，不需要新端点。

## 3. `title` 的必填与回退（需要 owner 确认）

Server 的 S1 答复是"`title` 可选、缺省回退 `playgroundId`"；owner 的最新规则是"界面不显示 id"。
客户端现状：缺 `title` 时显示「未命名课程」（不是 id）。

建议（二选一，需 server 明确）：

- **A（推荐）**：`title` 与 `runtime` 同级、新上传必填（trim 后 1–80 字符）；旧 commit 继续省略字段。
- B：保持可选；客户端永远显示「未命名课程」，发布端负责补齐。

## 4. Admin 用户列表

```http
GET /api/v1/admin/users
Authorization: Bearer <admin>
→ 200 {"users":[{"id","username","displayName","role","createdAt"}]}
```

- 仅 Admin；稳定排序（`role, id`）。Admin 视图的账号表目前是空的。
- `displayName` 可空（见 §6）；视图显示 `displayName ?? username`，不显示 id。
- `status`（正常/等待首次登录/停用）目前数据库没有；客户端已改为"没有就不显示状态"，不要求补。

## 5. Comment 作者名

`comments.comment_to_mapping` 增加可选 `authorUsername`（teacher 评论的作者；`ai`/`system` 为 `null`）：

```json
{"id":"…","submissionId":"…","authorUserId":"…","authorUsername":"张老师","source":"teacher","body":"…","createdAt":"…"}
```

学生点评抽屉现在只能显示 `source` 标签；教师端评语同理。若同时采用 §6 的 `displayName`，
则字段名统一为 `authorDisplayName`（二选一，不并列两套）。

## 6. `users.display_name`（可选）

现状：`users` 只有 `username`，各读端点用 `username` 当展示名。若产品需要真实姓名：

- `ALTER TABLE users ADD COLUMN display_name TEXT`（可空；空 = 回退 `username`），`SCHEMA_VERSION` 顺延；
- 注册与 Admin 创建用户接受可选 `displayName`（trim 后 1–40 字符、无控制字符）；
- `_user_payload`／submission `username`／member `username`／comment 作者字段统一改为
  `displayName ?? username`（或同时提供两个字段，但客户端只认"显示名"一个决定）。

不建议同时维护两份"名字"事实：要么就用 `username`，要么统一到 display name。

## 7. 兼容、顺序与部署

- 全部为**新增可选字段或新增端点**：旧客户端忽略，新客户端对缺失回退（客户端已实现）。
- 建议顺序：**§1b（教师资源池，当前唯一阻塞「发布→上课」闭环的项）** → §4（Admin 表） → §1（少 N 次请求）→ §5（点评作者）→ §2（玩具）→ §3/§6（决策项）。
- Web 端 `toy.entry` 必须部署在**独立 origin**（如 `courses.picopg.cn`）：同源 iframe 里的课程页
  能读写宿主 localStorage，这是部署要求，不是 server 代码改动。
- Web 课程 origin 还必须提供 `../pyodide/`（课程页用 `libs/**`、`../pyodide/` 引用运行时），
  桌面端由随包 runtime 覆盖 `pyodide/**`。这条要和 `toy.entry` 的部署一起定。

## 8. 验收

- bundle：`toy` 非法形状、路径越界/缺失、超限 → 各自 400 + reason；旧 bundle 不回归。
- catalog：有/无元数据两种 commit；Release 固定旧 commit 时展示旧元数据；`generated.rebuild` 后一致。
- admin users / members / comments：结构与现有授权矩阵一致（Admin 全量、教师仅自己班、学生仅自己）。
- `GET /api/v1/playgrounds`：教师/Admin 可读、学生 403、已归档不返回、稳定排序。
- 端到端：发布带 `toy/**` 的 bundle → catalog 投影带元数据 → 客户端卡片显示名称/封面。

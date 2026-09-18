## 功能
1. 分发已发布的课程内容，
2. 接收并保存学生交上来的作业。

## 接收到zip
1. 写一个 commit（tree 没变 = no-op，历史永不重写）
2. compare-and-swap 更新 refs/heads/playgrounds/<id>
3. 写/更新一行 playgrounds

学生 ─ GET /api/v1/catalog ─→ 读 manifest，只下本地缺的 blob ─→ 打开课程页
学生 ─ POST /api/v1/submissions ─→ 一行记录 + 一份目录副本





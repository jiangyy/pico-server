# Client / Server 审查与重构

审查日期：2026-10-21。范围：PicoPG 服务端与 Learn Python 的 `apps/client`，重点是提交、日志写入、反馈/目录/历史投影及其真实调用方。不是全仓库安全审计，也不把上一轮 AI 教学方案混入本轮重构。

## 总体判断

现有分层值得保留：ReplicaLog 保存不可变记录；Draft 处理多设备协调；Submission 固定源码版本；课程包拥有课程实现；分析 worker 异步产生结果。HTTP/UI 大多是这些能力的入口。服务端全套 16 个测试脚本、客户端 36 个测试在修改前通过。

主要维护成本来自同一规则散落在多个消费者，以及异步边界没有明确表达“哪一时刻的数据”和“刷新何时算完成”。这比文件长短或类/函数写法更值得优先处理。

## Requirement

同一提交在归档、教师查看和 AI 分析中必须指向同一学生、课程和 Revision；客户端入队后不允许调用方修改已接受的数据；密集同步通知不应排出等量全量读取，最后一次通知仍必须被覆盖。

## Existing Contract / Consumers

- `submission.requested` 引用 Revision，`requires` 表达依赖。真实消费者是 Submission worker、SubmissionReader、教师详情和 SubmissionMaterializer。
- `client.append` 进入每条 Log 的串行队列，完成后返回 Position。消费者包括草稿保存、提交请求和学习 evidence。
- 目录、提交历史和反馈各自拥有状态和生命周期，以 `ready` / `refresh()` 等待投影完成；同步事件会触发刷新。

## Classification / Ownership

提交引用、持久 Revision 和分析结果是不可变记录；当前编辑源码是 Cursor；目录/历史/反馈是可重建投影；写队列和刷新调度是私有机制。Submission 模块拥有提交解析与源码归属校验；Client writer 拥有入队快照；各投影仍拥有读取、状态、错误展示和 dispose。

## Problem

| 问题 | 代码证据 | 实际影响 |
|---|---|---|
| 提交解释重复且不一致 | `submission/worker.py`、`submission/read_model.py`、`review/submissions_view.py`、`learning_analysis/materializers.py` | 教师详情检查 draftId，worker/分析路径只检查 Principal；同一学生另一门课的源码可能被归档或分析为当前课 |
| 排队期间数据仍可变 | `apps/client/impl/client/writer.ts` 在异步读取 head 后才复制 data/requires | 总线的外层事件快照未固定嵌套数据；事件接收后修改对象，会改变持久化内容和依赖 |
| 三套同形刷新队列 | `submission/history.ts`、`playground/catalog.ts`、`learning-analysis/projection.ts` | 每个通知排一次完整读取；突发通知造成冗余工作和延迟，三处还要分别维护失败与关闭行为 |

## Minimal Contract Delta

1. 在服务端 Submission 模块提供不可变请求数据、统一 decoder 和源码解析。消费者复用它；错误在各边界转换为已有错误类型。归属不合法时不得调用归档器或模型。
2. writer 同步验证并深复制事件，在第一次异步等待之前固定 requestId、logId、requires 和 data；队列仅接收该快照。
3. 在 client 私有实现中提取刷新调度：同一时间最多一个读取，执行中来的通知合并成下一轮；调用者等待覆盖自己请求的那一轮。投影负责业务状态，调度器只负责排队与关闭。

保持 wire JSON、已有 Log ID、公开 Client API 与 `src/` 核心架构不变，无迁移步骤。共享模块只服务已经存在的消费者。

## Invariants

- 仍允许提交自己的分支 Revision、另一设备保存的同课 Revision；不能误要求 Revision replica 与提交设备相同。
- 未完成/失败提交仍能出现在列表中；读取列表不要求归档已成功。
- 每个 append 保留顺序和失败通知；无效 data 在入队前拒绝，拒绝不阻塞后续写入。
- refresh 不并发读；执行期间的新通知不会丢失；失败后可以再刷新；dispose 后不发布新投影。
- 目录/反馈读取失败时仍保留最后成功的值；不会把调度器做成第二份状态存储。

## Not Introduced

不引入通用仓储框架、跨语言 DTO 生成器、新事件总线、事件溯源重写或新的服务层。工作区较大但已有明确状态机；本轮不按行数机械拆文件。

## Verification

先新增跨课程提交拒绝、事件入队后修改、突发刷新/中途通知/失败恢复/dispose 的回归；确认旧代码可复现，再实现。运行服务端全套测试、客户端全套测试、TypeScript 检查和客户端构建。新测试必须被现有默认测试入口发现。

## Unresolved / 后续单独决策

- 课程版本与 exerciseKey 仍未由 Submission 本身完整表达，现由 evidence 补 versionId；这需要配合课程题目模型设计协议版本，不在维护性重构里猜字段。
- AI 反馈按结果到达顺序展示；现有重试也可能导致旧提交晚完成。应单独定义跨提交、跨设备以及不同分析版本的结果选择规则。
- 本轮合并刷新次数，但不改变每次全量读取的复杂度；长历史下是否需要增量投影，应以实际数据量和测量决定。

## 已实现与验证

- [Submission 协议实现](apps/server/submission/protocol.py)集中解析请求及校验源码归属，四条消费路径已接入；同学生跨课程提交由成功归档改为拒绝，同课跨设备仍成功。请求身份与结果状态分开建模，列表继续展示尚未完成的提交。
- [Client writer](../learn-python/apps/client/impl/client/writer.ts)在入队时固定整个请求；嵌套对象和依赖引用被调用方修改也不会改变入队内容。无效 JSON 值同步拒绝。
- [刷新调度](../learn-python/apps/client/impl/refresh.ts)由目录、历史、反馈共同使用。测试场景中，第一次读取未完成时连续收到 20 次通知，读取从 21 次降到 2 次；这是该突发场景的结果，不是整体性能基准。
- 先在旧实现复现上述失败，再验证修复。服务端 `./run-tests.sh` 共 17 个脚本通过；客户端 `npm run test:client` 共 44 个测试通过；`npm run check` 和 `npm run build:client` 通过。新增测试已由默认测试入口发现；修改的 Python 文件通过 Ruff。

未改网络 payload、核心 `src/` 或部署配置；未做浏览器视觉检查和线上部署。旧的错误归档不自动重写；本次校验约束后续处理及读取源码时的归属。

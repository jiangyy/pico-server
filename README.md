# PicoPG Replica Server

Server 的复制核心只充当一个参与 Log 复制的远端参与者。它持有 durable Logs，并通过与 Client RemotePeer 对称的四个操作参与同步：

~~~text
head(logId)
has(Position)
read(logId, after, limit)
append(logId, expectedHead, records)
~~~

Server 不解释 record 的 data 是代码、作业、AI 请求、分析结果还是 Playground 发布。领域 worker 从请求 Log 读取事实，完成工作后向自己拥有的结果 Log 追加事实；Client 随后通过同一套 Remote 机制拉回结果。

## 目录

~~~text
src/picopg/api/replica_log.py       Position、Record、Page、Store port
src/picopg/impl/file_replica_log.py 原子文件持久化
apps/server/protocol.py             HTTP RemotePeer adapter 与 SSE 唤醒
apps/server/playgrounds.py          包格式适配、内容寻址存储与 release 发布
apps/server/application.py          Playground HTTP 路由组装
apps/server/main.py                 组装和进程生命周期
~~~

## HTTP

~~~text
GET  /healthz
GET  /api/v1/replica/handshake
GET  /api/v1/replica/events
GET  /api/v1/replica/logs/{logId}/head
HEAD /api/v1/replica/logs/{logId}/records/{recordId}
GET  /api/v1/replica/logs/{logId}/records?after={recordId}&limit=128
POST /api/v1/replica/logs/{logId}/records

GET  /api/v1/catalog
POST /api/v1/playgrounds/{id}/versions
GET  /api/v1/playgrounds/{id}/versions/{versionId}/package
~~~

Replica 路由要求 Authorization: Bearer &lt;token&gt;。每个 Log 由启动参数绑定为 `read` 或 `append`：`read` 允许 Client 读取 server-owned Log；`append` 允许 Client 对 client-owned Log 查询 `head/has` 并 compare-and-append，但不允许下载记录内容。当前最小版本使用一个 server-wide token；接入多个互不信任的用户前，必须增加 provisioning，不能让 Client 自己声明权限。

`/events` 是允许丢失的 SSE 唤醒通道。它只通知 Log 可能变化，Client 首次连接和每次重连后都要比较 head；正确性不依赖事件回放。

Playground 发布不把 ZIP 塞进 Replica JSON。Server 先由当前包格式适配器校验内容，再以 ZIP 的 SHA-256 作为 Version id 原子落盘，最后向 server-owned `playground-releases` Log 追加 `playground.released`。只有 Log 追加成功后才通过 SSE 唤醒 Client；Client 先同步 release 事实，需要内容时再按 Version 下载包。

当前适配器接受 `application/zip`，要求包含 `picopg.json` 与 `course/index.html`。复制、发布与下载流程只依赖 `mediaType` 和不可变 Version id；未来切换 `.pico` 时替换包适配器，不需要修改 Replica Core 或 Client EventBus 链路。

POST body 与 Client 的 ReplicaLogStore.append 一致：

~~~json
{
  "expectedHead": null,
  "records": [
    {
      "id": "sha256:739461235b1f2584b68c8383876ccda00dd59caffa099d13461ec684153d516b",
      "logId": "work",
      "parent": null,
      "requires": [],
      "data": {"source": "print(17)"}
    }
  ]
}
~~~

结果为 {kind: accepted|current|changed, head}。accepted 只在数据已经原子持久化后返回；相同记录重试得到 current；不同后缀得到 HTTP 409 / changed，不会覆盖。

## 运行

~~~bash
PYTHONPATH=src python3 apps/server/main.py \
  --host 127.0.0.1 \
  --port 8001 \
  --data data \
  --binding work=append \
  --binding results=read
~~~

Token 优先读取 PICOPG_REPLICA_TOKEN，其次是 --token；两者都没有时生成 data/replica.token，权限为 0600。

## 当前不包含

- Object Store、Ref、Git 或 SQLite；
- LogDefinition、descriptor 或动态发现；
- Work、Submission、AI、Artifact 等其他领域 schema；
- 多写者 Log、merge 或 background scheduler；
- 多用户账号和 per-Log capability provisioning。

这些能力只有在真实 consumer 接入时才增加，不能进入 Replica Core 预占设计。

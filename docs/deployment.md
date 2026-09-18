# Deployment — Server on `picopg.cn`

运维手册：这台机器上跑什么、怎么更新、怎么发课、怎么收作业。

| 层 | 是什么 | 位置 | 谁启动 |
|---|---|---|---|
| 反代 | nginx + Let's Encrypt | `/etc/nginx/sites-enabled/picopg` | `nginx.service` |
| 应用 | 纯标准库 Python 3 | `~/picopg/apps/server/server.py` | user `picopg.service` |
| 代码 | git 工作树 | `~/picopg/` | 运维 pull |
| 数据 | SQLite + 裸仓库 + 缓存 + runtime | `~/picopg/data/` | 应用自己 |

这台机器只有 2 GiB 内存、无 swap，不要装 VS Code Remote 之类的东西（9 月 16 日的 OOM 就是它）。

## 0. Operator token

所有写端点由同一个 token 授权，存在 unit 的环境里，不入库：

```bash
ssh picopg.cn
mkdir -p ~/.config/systemd/user/picopg.service.d
printf '[Service]\nEnvironment=PICOPG_OPERATOR_TOKEN=%s\n' "$(openssl rand -hex 24)" \
  > ~/.config/systemd/user/picopg.service.d/token.conf
chmod 600 ~/.config/systemd/user/picopg.service.d/token.conf
systemctl --user daemon-reload && systemctl --user restart picopg.service
cat ~/.config/systemd/user/picopg.service.d/token.conf   # 抄进密码管理器
```

缺 token 或短于 16 字节时服务拒绝启动（否则每个写端点都会变成公开的）。

## 1. 更新应用

线上拿不到 GitHub，所以打包传过去：

```bash
# 本机
git -C ~/Documents/picopg bundle create /tmp/picopg.bundle <旧sha>..master
scp /tmp/picopg.bundle picopg.cn:/tmp/

# 线上
ssh picopg.cn 'cd ~/picopg && git fetch /tmp/picopg.bundle master \
  && git merge --ff-only FETCH_HEAD && rm /tmp/picopg.bundle \
  && systemctl --user restart picopg.service'
```

`git status` 必须干净；脏文件是运维遗留，先 stash。

### 1.1 schema 变了就重建 app.db

`app.db` 只有一个版本号，版本不符时启动即报错（不会半迁移）。重建只影响数据库，Git 历史留在 `data/picopg.git/`：

```bash
ssh picopg.cn 'cd ~/picopg && systemctl --user stop picopg.service \
  && mv data/app.db /tmp/app.db.old && systemctl --user start picopg.service'
```

起来后 catalog 会暂时为空（`playgrounds` 表是空的），用第 2 节重新登记每个 id 即可，commitId 不变。

如果 `data/picopg.git/` 或 `data/runtime/` 也一起丢了（两者都不在数据库里），内容就得重新发布：Git 历史没了，`commitId` 会变，客户端把新课当新版本重下。

### 1.2 nginx 必须允许大 body

nginx 默认只接受 1 MiB 请求体，课程 ZIP 会直接吃到 `413`（app 自己的上限是 10 MiB）。在站点的 `location /` 里加一行再 reload：

```bash
sudo sed -i 's|# *client_max_body_size 50m;|client_max_body_size 50m;|' /etc/nginx/sites-available/picopg
sudo nginx -t && sudo systemctl reload nginx
curl -sS -o /dev/null -w "%{http_code}\n" -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/zip" -H "Idempotency-Key: probe" \
  --data-binary @/dev/null https://picopg.cn/api/v1/playgrounds/hello/commits   # 400/201 都说明通了，413 说明还没生效
```

判断卡在哪一层：`--url http://127.0.0.1:8001/...` 直连应用能成功、走域名返回 413，就是 nginx 限额，不是应用的。

## 2. 发一门课

```bash
# 本机：构建（不联网）
cd ~/Documents/learn-python && node scripts/publish.mjs hello
ls dist/publish/hello.zip dist/publish/runtime
```

```bash
# 线上：登记（新 id 才需要）+ 上传
TOKEN=$(sed -n 's/^Environment=PICOPG_OPERATOR_TOKEN=//p' \
  ~/.config/systemd/user/picopg.service.d/token.conf)
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"id":"hello"}' https://picopg.cn/api/v1/playgrounds
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/zip" \
  -H "Idempotency-Key: hello-$(date +%s)" --data-binary @dist/publish/hello.zip \
  https://picopg.cn/api/v1/playgrounds/hello/commits
```

runtime 只在版本变化时上传一次（18 MiB）：

```bash
scp -r dist/publish/runtime/. picopg.cn:~/picopg/data/runtime/
```

## 3. 收作业

```bash
TOKEN=$(sed -n 's/^Environment=PICOPG_OPERATOR_TOKEN=//p' \
  ~/.config/systemd/user/picopg.service.d/token.conf)
curl -fsS -H "Authorization: Bearer $TOKEN" https://picopg.cn/api/v1/submissions \
  | jq '.submissions[] | {id, student, playgroundId, createdAt}'
curl -fsS -H "Authorization: Bearer $TOKEN" https://picopg.cn/api/v1/submissions/<id> \
  | jq '.submission.files'
```

提交只追加，服务器不把它写进 Git。

## 4. 每次重启/发布后

```bash
systemctl --user status picopg.service        # active (running)
free -h                                       # used ≤ 600 MiB
curl -sS -o /dev/null -w "%{http_code}\n" https://picopg.cn/api/v1/catalog
curl -sS -o /dev/null -w "%{http_code}\n" https://picopg.cn/api/v1/bootstrap
```

备份 `data/app.db`、`data/picopg.git/`、`data/artifacts/` 三件套；`data/generated/` 删了会自己重建。

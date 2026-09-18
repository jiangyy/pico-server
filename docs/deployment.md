# Deployment guide — Server on `picopg.cn`

This file is the only thing the operator needs to read to keep `picopg.cn`
alive and to ship a new Playground from the `learn-python` repo.

## What is running on the server

| Layer | What | Where | Started by |
|---|---|---|---|
| Reverse proxy | nginx 1.x + Let's Encrypt certs | `/etc/nginx/sites-enabled/picopg` | systemd (`nginx.service`) |
| Application | Python 3 standard library only, no framework | `/home/xiaoci/picopg/apps/server/server.py` | user systemd (`picopg.service`) |
| Code | git working tree | `/home/xiaoci/picopg/` | operator pulls |
| Mutable data | SQLite + bare git repo + generated cache + artifacts + runtime | `/home/xiaoci/picopg/data/` | the application |

Nothing else belongs on this machine: the box has 2 GiB of RAM and no swap,
and the original 22:02 OOM on Sep 16 was caused by VS Code Remote Node
processes consuming ~290 MiB after the xiaoci session ended.

## 0. The operator token

Every write endpoint is authorized by one token, supplied to the service and
never stored in the database. Generate it once:

```bash
openssl rand -hex 24          # at least 16 bytes; store it in your password manager
```

Put it in the unit's environment, not in the repository:

```bash
ssh picopg.cn
mkdir -p ~/.config/systemd/user/picopg.service.d
cat > ~/.config/systemd/user/picopg.service.d/token.conf <<'EOF'
[Service]
Environment=PICOPG_OPERATOR_TOKEN=<the token>
EOF
chmod 600 ~/.config/systemd/user/picopg.service.d/token.conf
systemctl --user daemon-reload
systemctl --user restart picopg.service
```

The Server refuses to start when the token is missing or shorter than 16
bytes, because without it every write endpoint would be public.

## 1. Keep the application up to date

The Server is plain Python on the standard library, so "deployment" means
"git fetch + restart":

```bash
ssh picopg.cn
cd ~/picopg
git fetch origin master
git merge --ff-only origin/master
systemctl --user restart picopg.service
systemctl --user status picopg.service
curl -sS -o /dev/null -w "%{http_code}\n" https://picopg.cn/api/v1/bootstrap
```

`git status` must be clean before the merge. If the working tree is dirty,
the dirty files are operator leftovers; stash them, merge, drop the stash,
restart.

### 1.1 A data directory from the older model must be recreated

There are no accounts, classes, or releases any more, and the schema is one
version describing three tables. A database written by the previous model is
rejected at startup instead of being half-migrated. Move it aside and start
clean:

```bash
ssh picopg.cn
cd ~/picopg
systemctl --user stop picopg.service
mv data data.previous
systemctl --user start picopg.service   # recreates data/ and the caches
curl -sS -o /dev/null -w "%{http_code}\n" https://picopg.cn/api/v1/catalog
```

Courses are published again from `learn-python` (section 2); the repository
history is not copied forward.

## 2. Ship a new Playground from `learn-python`

The supported helper is `node scripts/publish.mjs <id>`; it emits the course
ZIP and the shared runtime overlay from the same build. It does not upload.

### 2.1 Build the artifacts locally

```bash
cd ~/Documents/learn-python
node scripts/publish.mjs hello
ls dist/publish/hello.zip dist/publish/runtime
```

### 2.2 Push the runtime once per host (almost never)

The runtime is served from `data/runtime/`. Re-upload it only when the runtime
version changes:

```bash
scp -r dist/publish/runtime/. \
  picopg.cn:/home/xiaoci/picopg/data/runtime/
```

### 2.3 Pre-create Playground metadata if it is new

The commit endpoint requires the Playground to exist:

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id":"hello"}' \
  https://picopg.cn/api/v1/playgrounds
```

Existing Playgrounds skip this step.

### 2.4 Push every new Playground ZIP

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $OPERATOR_TOKEN" \
  -H "Content-Type: application/zip" \
  -H "Idempotency-Key: hello-$(date +%s)" \
  --data-binary @dist/publish/hello.zip \
  https://picopg.cn/api/v1/playgrounds/hello/commits
```

The Server validates every member of the ZIP: only `course/**`,
`starter/**`, and `picopg.json` at the root are allowed, no absolute paths,
no `..`, no symlinks, no unknown schemas or runtimes. An unchanged tree is a
no-op (`noOp: true`) and returns the current `commitId`.

### 2.5 Verify the publish landed

```bash
curl -fsS https://picopg.cn/api/v1/catalog | jq '.catalog[] | select(.playgroundId=="hello")'
curl -fsS https://picopg.cn/api/v1/playgrounds/hello/commits/<commitId>/manifest | jq '.manifest.files | length'
```

If `hello` is missing, the upload silently turned into a `noOp`: the tree
matched the existing HEAD. Change the bundle and upload again with a fresh
`Idempotency-Key`.

## 3. Read what students handed in

```bash
curl -fsS -H "Authorization: Bearer $OPERATOR_TOKEN" \
  https://picopg.cn/api/v1/submissions | jq '.submissions[] | {id, student, playgroundId, createdAt}'

curl -fsS -H "Authorization: Bearer $OPERATOR_TOKEN" \
  https://picopg.cn/api/v1/submissions/<id> | jq '.submission.files'
```

Submissions are append-only: there is no update, delete, or status, and the
Server never copies a submission into the Git repository.

## 4. Operations checklist

After every restart or deploy:

```bash
systemctl --user status picopg.service   # Active: active (running)
free -h                                  # used ≤ 600 MiB, no swap pressure
ss -lntp | grep ':8001 '                 # python3 listening on 8001
curl -sS -o /dev/null -w "%{http_code}\n" https://picopg.cn/api/v1/bootstrap
curl -sS -o /dev/null -w "%{http_code}\n" https://picopg.cn/api/v1/catalog
```

Back up `data/app.db`, `data/picopg.git/`, and `data/artifacts/` as one set.
`data/generated/` is disposable and rebuilds itself.

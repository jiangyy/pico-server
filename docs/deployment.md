# Deployment guide — Server on `picopg.cn` and the Web bundle

This file is the only thing the operator needs to read to keep `picopg.cn`
alive and to ship a new Playground from the `learn-python` repo. It
deliberately stays short and references the existing automation; do not
rebuild anything that already exists.

## What is running on the server

| Layer | What | Where | Started by |
|---|---|---|---|
| Reverse proxy | nginx 1.x + Let's Encrypt certs | `/etc/nginx/sites-enabled/picopg` | systemd (`nginx.service`) |
| Application | Python 3 standard library only, no framework | `/home/xiaoci/picopg/apps/server/server.py` | user systemd (`picopg.service`) |
| Code | git working tree | `/home/xiaoci/picopg/` | operator pulls |
| Mutable data | SQLite + bare git repo + generated cache + artifacts | `/home/xiaoci/picopg/data/` | the application |
| Web client bundle | built from `learn-python`, dropped in by the operator | (to be added — see §2) | operator |

Nothing else belongs on this machine. VS Code Remote, the `todo/` queue,
and any other development artifact must not be reinstalled: the box has 2 GiB
of RAM and no swap, and the original 22:02 OOM on Sep 16 was caused by VS Code
Remote Node processes consuming ~290 MiB after the xiaoci session ended.

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

If the remote is unreachable from the server (no SSH key to GitHub), build a
bundle locally and ship it:

```bash
# Local
git -C ~/Documents/picopg bundle create /tmp/picopg.bundle <old-sha>..<new-sha>
scp /tmp/picopg.bundle picopg.cn:/tmp/

# Remote
ssh picopg.cn
cd ~/picopg
git fetch /tmp/picopg.bundle HEAD
git merge --ff-only FETCH_HEAD
rm /tmp/picopg.bundle
systemctl --user restart picopg.service
```

### 1.1 Create the first Admin

A fresh data directory has no accounts and no HTTP route can mint an Admin
without an existing Admin, so the first one comes from the operator CLI. The
Server must have initialized `data/` first (a normal start does that):

```bash
ssh picopg.cn
cd ~/picopg
python3 apps/server/maintenance.py create-admin \
    --data-dir data \
    --username admin \
    --password '<choose a strong one>'
```

It prints `{"status": "ok", ...}` on success and
`{"error": "admin_exists"}` (exit 1) once any Admin exists — the bootstrap is
one-shot and never a second door into a running deployment. Only an existing
*Admin* blocks it; student accounts registered through the public API do not.

## 2. Serve the Web client bundle (`picopg.cn/`)

The Server serves the bundle itself. `GET /` and the three assets it
references come from `<data>/web/`, so no nginx change is required: the
existing `location / { proxy_pass http://127.0.0.1:8001; }` already forwards
every path to the Server, and the API and the Web product share one origin
(which the bundle's `connect-src 'self'` CSP requires).

Install or update the bundle:

```bash
cd ~/Documents/learn-python
node apps/picopg-web/build.mjs
scp apps/picopg-web/dist/{index.html,main.js,picopg.css,pico.svg} \
    picopg.cn:/home/xiaoci/picopg/data/web/
# `scripts/publish.mjs` also produces `dist/publish/runtime/`; copy that
# directory to `data/web/runtime/` when the browser runtime changes.
scp -r dist/publish/runtime \
    picopg.cn:/home/xiaoci/picopg/data/web/
ssh picopg.cn 'systemctl --user restart picopg.service'
```

`WEB_BUNDLE` in `apps/server/server.py` is a closed path table — `index.html`,
`main.js`, `picopg.css`, `pico.svg`. A file in `data/web/` that is not in the
table is unreachable by URL, and no caller-supplied name is ever joined onto
the directory. An absent bundle keeps the historical placeholder on `/` so an
un-provisioned host still answers its health probe. Each asset carries its
content hash as an `ETag` and a five-minute `max-age`, so a reload reuses the
bytes already in the browser.

To verify:

```bash
curl -sSI https://picopg.cn/          # 200, text/html
curl -sSI https://picopg.cn/main.js   # 200, text/javascript, ETag + max-age
curl -sS  https://picopg.cn/api/v1/bootstrap   # 200
```

## 3. Ship a new Playground from `learn-python`

The Server accepts a validated ZIP through `POST /api/v1/playgrounds/{id}/commits`.
The supported helper is `node scripts/publish.mjs <id>`; it emits the course ZIP
and the shared runtime overlay from the same build.

### 3.1 Build the artifacts locally

```bash
cd ~/Documents/learn-python
node scripts/publish.mjs hello --requires-submission
ls dist/publish/hello.zip dist/publish/runtime
```

The ZIP contains only `picopg.json` and `course/index.html`; runtime files are
served from `data/web/runtime/`. The `requiresSubmission` flag is explicit in
`picopg.json` and defaults to `false` when the flag is omitted.

### 3.2 Push the runtime once per host (almost never)

The runtime is served from `data/web/runtime/` for the same-origin course URL.
Re-upload it only when the runtime version changes:

```bash
scp -r dist/publish/runtime \
  picopg.cn:/home/xiaoci/picopg/data/web/
```

### 3.3 Push every new Playground ZIP

```bash
ADMIN_TOKEN=…   # sessionToken from an Admin login
curl -fsS -X POST \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/zip" \
  -H "Idempotency-Key: hello-$(date +%s)" \
  --data-binary @dist/publish/hello.zip \
  https://picopg.cn/api/v1/playgrounds/hello/commits
```

The Server validates every member of the ZIP: only `course/**`,
`starter/**`, and `picopg.json` at the root are allowed, no absolute
paths, no `..`, no symlinks, no unknown schemas or runtimes. A second
upload with the same `<commitId>` and the same `Idempotency-Key` is a
no-op (`noOp: true` in the response); a second upload with the same
`<commitId>` and a fresh key is also a no-op because the tree is
unchanged.

### 3.4 Pre-create Playground metadata if it is new

The publish endpoint requires the Playground to already exist in the
database. For a brand-new `hello`, register it once:

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id":"hello"}' \
  https://picopg.cn/api/v1/playgrounds
```

Existing Playgrounds (`hello`, `hanoi`, etc. that are already in the DB)
skip this step.

### 3.5 Verify the publish landed

```bash
TEACHER_TOKEN=…   # a real Teacher's sessionToken
curl -fsS \
  -H "Authorization: Bearer $TEACHER_TOKEN" \
  https://picopg.cn/api/v1/playgrounds | jq .
```

The returned `playgrounds[]` list must include `hello` with the right
`commitId`, `title?`, `summary?`, and `requiresSubmission` (driven by
`picopg.json`). If `hello` is missing, the ZIP upload silently turned
into a `noOp` — either the idempotency key collided with an earlier
attempt or the bundle tree matched the existing `HEAD`. Re-check the
`Idempotency-Key` header and re-upload.

## 4. Operations checklist

After every restart or deploy:

```bash
systemctl --user status picopg.service   # Active: active (running)
free -h                                  # used ≤ 600 MiB, no swap pressure
ss -lntp | grep ':8001 '                 # python3 listening on 8001
curl -sS -o /dev/null -w "%{http_code}\n" https://picopg.cn/api/v1/bootstrap
curl -sS -o /dev/null -w "%{http_code}\n" https://picopg.cn/   # 200 if Option A or B is wired
```

After every cleanup:

```bash
pgrep -af 'vscode|code-server' | grep -v pgrep   # must be empty
ls /home/xiaoci/picopg/todo 2>&1                 # must be "No such file"
```

If any of those fail, the box has regressed toward dev mode and should be
restored before the next deploy.

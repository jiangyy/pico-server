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

## 2. Serve the Web client bundle (`picopg.cn/`)

The Server's `GET /` currently returns the literal placeholder
`"Pico service is running\n"`. The Web client must be served from this host
name so users land on the actual product, not on a debug line. Pick **one**
of the two approaches below — both end at the same user experience.

### Option A — Server owns the static bundle (recommended)

Add one handler in `apps/server/server.py` that serves files from
`data/web/` for the few paths the Web build produces (`/`, `/main.js`,
`/picopg.css`, `/pico.svg`, `/auth.css`, plus `/{base,login}` shells), and
falls through to the existing API routing for everything else. The Server
runs in the same process as the API, so there is no CORS, no extra hop, and
the deploy contract is "drop the four files into `data/web/`".

Pros: single source of truth, no nginx tweaks, deploy is a copy.

Cons: a small change to `server.py`. The Server already serves
`GET /api/v1/bootstrap` from the generated cache; the static handler is the
same shape, just with bytes from disk.

### Option B — nginx serves the bundle directly

Add a second `location` block to `/etc/nginx/sites-enabled/picopg` that
serves `/var/www/picopg/web/` for `/`, `/main.js`, `/picopg.css`,
`/pico.svg`. Everything else keeps proxying to `127.0.0.1:8001`:

```nginx
server {
    # ... existing server block ...

    location = / { root /var/www/picopg/web; try_files /index.html =404; }
    location ~ ^/(main\.js|picopg\.css|pico\.svg|auth\.css)$ {
        root /var/www/picopg/web;
        try_files $uri =404;
        expires 1h;
        add_header Cache-Control "public, max-age=3600";
    }

    location / {
        proxy_pass http://127.0.0.1:8001;
        # ... existing proxy_set_header lines ...
    }
}
```

Pros: zero Python changes, cache headers explicit, no impact on the API.

Cons: two deploy steps (copy + reload nginx), two directories to keep in
sync.

The Web build prints the exact four filenames it produced. Copy them into
whichever root was chosen:

```bash
scp apps/picopg-web/dist/{index.html,main.js,picopg.css,pico.svg} \
    picopg.cn:/var/www/picopg/web/
# then, for option B only:
ssh picopg.cn 'nginx -t && systemctl reload nginx'
```

## 3. Ship a new Playground from `learn-python`

The Server speaks the bundle upload contract that `learn-python/scripts/publish.mjs`
already produces, so the operator's job is to ship bytes that match.

### 3.1 Build the artifacts locally

```bash
cd ~/Documents/learn-python
npm run publish
ls dist/publish/playgrounds/hello
ls dist/publish/runtimes/pico-runtime-1
```

`dist/publish/playgrounds/hello/<commitId>/course/index.html` is the
playground itself; `dist/publish/runtimes/pico-runtime-1/runtime.json` is
the runtime manifest. Each one is **content-addressed**: the `<commitId>`
is `sha256(sorted paths + sha256 + size)` and is the only safe identifier
to put on the wire.

### 3.2 Push the runtime once per host (almost never)

The runtime is bundled into the desktop package on the client side and is
served by the Server for browsers. The Server stores it under
`data/artifacts/`. Re-upload only when the runtime version changes:

```bash
cd dist/publish/runtimes/pico-runtime-1
tar -cf runtime.tar runtime.json libs/ pico.js world.js pico.css pico.svg pyodide/
ADMIN_TOKEN=…   # from server operator
curl -fsS -X POST \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Idempotency-Key: runtime-$(cat runtime.json | jq -r .id)" \
  --data-binary @runtime.tar \
  https://picopg.cn/api/v1/artifacts
```

### 3.3 Push every new Playground ZIP

```bash
cd dist/publish/playgrounds/hello
PLAYGROUND_DIR=$(ls -d */ | head -1 | tr -d '/')    # <commitId>
zip -rq "$PLAYGROUND_DIR.zip" "$PLAYGROUND_DIR"
curl -fsS -X POST \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/zip" \
  -H "Idempotency-Key: hello-$PLAYGROUND_DIR" \
  --data-binary @"$PLAYGROUND_DIR.zip" \
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
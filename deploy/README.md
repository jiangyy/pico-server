# Single-instance deployment

This directory is the production contract for the controlled classroom pilot. It serves the built student and teacher entries through nginx and runs one PicoPG process through the user systemd manager.

## Install

1. Copy `deploy/picopg.service` to `~/.config/systemd/user/picopg.service`.
2. Copy the example JSON files to `config/principals.json` and `config/submissions.json`, replace every example secret/path, and keep both files readable only by the service user.
3. Create `~/.config/picopg/server.env` with `PICOPG_REPLICA_TOKEN=<random-secret>` and mode `0600`.
4. Build the web repository with `npm run build` and copy its complete `dist/` directory to `/var/www/picopg/`. This includes `/client/` and `/teacher/`.
5. Install the nginx configuration, obtain the certificate, then run `nginx -t` before reloading nginx.
6. Run `systemctl --user daemon-reload && systemctl --user enable --now picopg`.

The production service deliberately omits `--insecure-cookies`. Browser Sessions therefore require HTTPS.

## Verify the deployed workflow and restart

Publish a dedicated Playground that is not used by students, map it to an initialized Git repository in `config/submissions.json`, and give the smoke principal the `playgrounds`, `drafts`, and `submissions` capabilities. Export these variables without putting secrets in shell history:

- `PICOPG_SMOKE_BASE_URL`: the public HTTPS origin;
- `PICOPG_SMOKE_ACCESS_CODE`: access code for the dedicated smoke principal;
- `PICOPG_SMOKE_PLAYGROUND_ID`: the dedicated published Playground and repository mapping;
- `PICOPG_REPLICA_TOKEN`: operator token.

Then run:

```sh
deploy/restart-smoke.sh
```

The wrapper starts from a healthy service, creates a unique Revision, observes its SSE notification, drives the Draft worker, completes a Submission through the configured Git tag writer, adds a teacher comment, and confirms that the student can read it. It then restarts the service and verifies the same Session identity, Catalog package, Draft decision, exact submitted Revision, Git tag, comment, static entries, cookie policy, SSE prelude, and logout.

The temporary state file contains a live Session token. The wrapper sets `umask 077`, creates the file with `mktemp`, and removes it on exit. Run this smoke after every nginx, service, configuration, package, or server release. Each run intentionally leaves immutable smoke facts and one Git tag in the dedicated smoke Playground.

## Backup and restore drill

For a consistent single-instance snapshot, stop the writer while the archive is created:

```sh
systemctl --user stop picopg
python3 deploy/backup.py create --data "$HOME/picopg/data" --output "$HOME/backups/picopg-$(date +%F-%H%M%S).tar.gz"
systemctl --user start picopg
```

Use a shell trap in operator automation so a failed backup still restarts the service. The archive must be stored outside `data/` and copied off-host.

Restore only into an absent or empty directory; the command refuses to overwrite live data:

```sh
python3 deploy/backup.py restore --archive /path/to/snapshot.tar.gz --target /tmp/picopg-restored
```

Verify `/tmp/picopg-restored`, stop the service, move the current `data` directory aside, move the verified restore into its place, start the service, and run `deploy/restart-smoke.sh`. Keep the old directory until the classroom data has been checked.

`./run-tests.sh tests.test_backup` performs a local create/restore comparison and verifies path-traversal refusal. It does not replace an off-host restore drill.

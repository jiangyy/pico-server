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

`PYTHONPATH=src python3 -m unittest tests.test_backup` performs a local create/restore comparison and verifies path-traversal refusal. It does not replace an off-host restore drill.

## Pilot checklist (one class)

Run through this before the first lesson and again after every release. Each item names how to check it.

### Release order

1. Deploy the server first, then publish **every** course from the web repository (`node scripts/publish.mjs --all`, then `scripts/upload.mjs`). Packages carry the knowledge map in their outline; until one is published the learner profile has no concept labels and does not migrate old concept ids.
2. Rebuild and copy `dist/` (it includes `/teacher/`), then run `deploy/restart-smoke.sh`.
3. Before every release, run `npm run smoke:pilot` in the web repository. It starts this server from source and drives two devices of one student and a teacher through publish, update, offline edits and the divergence choice, learning evidence to profile, submission to Git tag, review, and a restart.

The submission repositories in `config/submissions.json` only need `git init`; the first submission becomes the root commit.

### Accounts and the class

- One account per student and one per teacher in `config/principals.json`. Teachers need no capabilities. Access codes are random and handed out on paper, never reused.
- Write the class roster through the operator endpoint (it replaces the whole roster each time):

  ```sh
  curl -fsS -X POST "$PICOPG_SMOKE_BASE_URL/api/v1/operator/classes/<class-id>" \
    -H "Authorization: Bearer $PICOPG_REPLICA_TOKEN" -H "Content-Type: application/json" \
    -d '{"teachers": ["<teacher>"], "students": ["<student>", "..."]}'
  ```

- Check: the teacher signs in at `/teacher/`, sees the class under 班级学情, and sees no other class's students.

### AI

- `PICOPG_AI_BASE_URL`, `PICOPG_AI_MODEL`, `PICOPG_AI_API_KEY` set in `server.env`; `PICOPG_AI_PRINCIPALS` lists the pilot students **and the teacher** (comment drafts use the same gate).
- `PICOPG_AI_HOURLY` (requests per principal, default 120) and `PICOPG_AI_DAILY_TOKENS` (whole server) set to what the pilot may spend.
- Confirm the model provider's content filtering and where it stores prompts; students' code and questions are sent to it.
- Rehearse with a real model, 10–20 times each: a question answered with `ask_student` and judged with `assess`; a practice handed in, failed, fixed and handed in again; "帮我整理一下知识点"; an AI comment draft in the teacher workbench. The AI must not read the learner profile out to the student.

### Students and guardians

- Written notice to guardians, signed before accounts are handed out: what is recorded (code, questions, answers, practice results), that code and questions are sent to the named model provider, who can see it (the class teacher), how long it is kept, and how to ask for deletion.
- Tell students that the AI can be wrong and that the teacher reads their conversations' results.

### During the pilot

- Daily: `journalctl --user -u picopg` for `learner profile failed`, `course agent` and `ai.limited` lines.
- Weekly: backup off-host (see above), and look at 班级学情 with the teacher — whether shaky/solid matches what the teacher sees in class is the check on the profile rules.

# HTTP conventions

## General rules

- Base path: `/api/v1`.
- JSON uses UTF-8 and camelCase.
- Timestamps are UTC RFC 3339 strings.
- IDs, Git commit IDs, and cursors are opaque strings.
- Unknown JSON response fields must be ignored by clients.
- Successful creation returns `201`; successful reads and updates return `200`
  unless the endpoint explicitly returns `204`.

## Authentication

Use `Authorization: Bearer <sessionToken>`. The token is shown once; Server
stores only its hash. Content marked `release_only` is authenticated like any
other protected resource.

## Idempotency

Requests that create immutable application objects require an
`Idempotency-Key` header:

- content publish
- Release creation
- Submission creation
- Comment creation

The same actor, endpoint, key, and body return the original result. Reusing a
key with a different body returns `409`.

Git ref updates use compare-and-swap. Work sync also carries `baseCommitId` in
the request body.

## Errors

```json
{
  "error": {
    "code": "work_head_changed",
    "message": "Work changed on the server",
    "details": {
      "currentHeadCommitId": "opaque"
    }
  }
}
```

Stable `code` values are for programs; `message` is for people. Do not branch
on message text.

| Status | Meaning |
| --- | --- |
| `400` | malformed request |
| `401` | missing or invalid session |
| `403` | authenticated but not authorized |
| `404` | absent or deliberately hidden resource |
| `409` | stale head or idempotency conflict |
| `413` | request or unpacked bundle too large |
| `422` | well-formed but invalid content |
| `429` | rate limited |

## Collections

Cursor collections return:

```json
{
  "items": [],
  "nextCursor": "opaque-or-null"
}
```

`/changes` orders eligible events by `(availableAt, id)`. Its opaque cursor is a
watermark in that order, not an event ID. Events with future `availableAt` are
not skipped when the cursor advances. Committing items and `nextCursor`
together makes retries safe.

## Minimum endpoints

### Content and runtime

```text
GET  /bootstrap
GET  /playgrounds/{playgroundId}/commits/{commitId}
GET  /playgrounds/{playgroundId}/commits/{commitId}/files/{relativePath}
POST /playgrounds/{playgroundId}/commits
GET  /runtimes/{runtimeVersion}
GET  /runtimes/{runtimeVersion}/files/{relativePath}
```

`GET /bootstrap` is the public, non-archived Playground index. A signed-in
Student combines it with `GET /releases`; it is not a per-user cache.

### Work and learning

```text
GET  /works
GET  /works/{playgroundId}
PUT  /works/{playgroundId}
GET  /works/{playgroundId}/commits
GET  /works/{playgroundId}/commits/{commitId}
POST /works/{playgroundId}/restore
POST /works/{playgroundId}/upgrade

GET  /releases
POST /releases
GET  /submissions
POST /submissions
GET  /submissions/{submissionId}/comments
POST /submissions/{submissionId}/comments
GET  /changes?after={cursor}
```

`/works/{playgroundId}/upgrade` may return `501` until course upgrade is added
after v0.1.

### Account and administration

```text
POST /auth/register
POST /auth/login
POST /auth/logout
POST /auth/password
GET  /me

GET  /teacher/students
GET  /teacher/playgrounds

GET|POST|PATCH /admin/users
PUT|DELETE     /admin/teacher-students/{teacherId}/{studentId}
PATCH          /admin/playgrounds/{playgroundId}
```

Endpoint bodies and response shapes should expose only the fields needed by
the corresponding flow; they must preserve the invariants in `core.md`.

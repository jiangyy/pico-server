# Server contract

## Responsibilities

Server:

- authenticates users and enforces roles and relationships;
- imports publisher bundles into private Git storage;
- serves authorized immutable content and runtime artifacts;
- performs Work Git operations;
- stores Releases, Submissions, Comments, and ChangeEvents transactionally;
- exposes the HTTP contract in [api-conventions.md](api-conventions.md).

It does not run student Python or treat generated manifests as authority.

## Storage

```text
data/
  picopg.git/          private bare Git repository
  app.db               application database
  artifacts/           immutable runtime and EXE files
  generated/
    bootstrap.json     atomically replaced public catalog cache
```

Production may replace SQLite or local artifact files without changing the
domain model. Do not split `picopg.git` until measured repository size or lock
contention requires it.

## Bundle import

`picopg.json` contains only versioned content metadata:

```json
{
  "schemaVersion": 1,
  "title": "Loops",
  "runtimeVersion": "pyodide-0.29.0",
  "entryPath": "course/index.html"
}
```

The route's `playgroundId` is the stable identity. Access and archival state
belong to `PlaygroundCatalog`, not the bundle.

Before writing Git objects, Server rejects:

- absolute paths, `..`, links, duplicate normalized paths, and special files;
- files outside `course/**`, `starter/**`, and `picopg.json`;
- unsupported schema/runtime versions;
- compressed, unpacked, file-count, and per-file limit violations.

Import writes objects first and moves the Playground ref last with
compare-and-swap. A new catalog row is created as `release_only`. Commit
manifest and public bootstrap generation happen after the ref move and are
retryable; a crash can leave a stale cache, never a false source of truth.

## Work writes

Server accepts only the editable `work/**` snapshot. It builds the full Git
tree itself, preserving publisher and server-owned paths. It verifies ownership,
authorization, file limits, and `baseCommitId`, then compare-and-swaps the Work
ref.

All repository subprocess arguments and paths are server-generated or strictly
validated. The bare repository is never exposed over native Git transport.

## Authorization-sensitive reads

A content commit is readable only when at least one is true:

- it is visible through a public, non-archived catalog entry;
- the student has an open Release pinned to it;
- it is referenced by the requesting student's own Work;
- it is referenced by a Submission the teacher is authorized to review;
- the caller is an authorized publisher or policy administrator.

Possessing a commit ID is not authorization.

## Database transactions

Release, Submission, and Comment creation insert their `ChangeEvent` in the
same transaction. A Release event's `availableAt` is no earlier than `openAt`.
Idempotency records are also committed with the result.

Git and the database cannot share a transaction. Operations therefore make
Git changes independently retryable and write database references only after
the target Git object exists. A repair command may rebuild catalog heads and
manifest caches from Git refs; it must not invent Releases or Submissions.

## Deployment artifacts

Server publishes immutable runtime and EXE artifacts by version. It also serves
small mutable indexes that point to those immutable files. Historical runtimes
needed by installed Work remain downloadable while that Work is supported.

Mutable index replacement uses write-to-temp, fsync where supported, then
rename. Clients still verify declared size and SHA-256.

## Operations

- Back up `picopg.git`, the database, and artifacts as one recovery set.
- Run startup checks for database schema, Git ref validity, writable storage,
  and artifact index integrity.
- Log actor, operation, target ID, result, and request ID without session tokens
  or student source contents.
- Provide repair commands for cache regeneration and dangling Git object
  inspection before adding background orchestration.

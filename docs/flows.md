# End-to-end flows

This file applies the model in [core.md](core.md).

## 1. Publish content

1. `learn-python` builds one Playground into `course/**` and `starter/**`.
2. CI packs those files plus `picopg.json` as a gzip bundle.
3. CI uploads the bundle with a publisher credential and idempotency key.
4. Server validates paths, limits, metadata, and `runtimeVersion`.
5. Server writes Git objects, creates a commit, and compare-and-swaps
   `refs/heads/playgrounds/<playgroundId>`.
6. Server regenerates the commit manifest and atomically replaces the public
   `bootstrap.json` cache.

Publishing never grants access. A new catalog entry defaults to
`release_only`; Admin changes access separately. Retrying the same bundle is a
no-op when the ref already points to the same tree.

## 2. Release a Playground

1. Teacher lists non-archived Playground catalog entries.
2. Teacher creates an immutable Release pinned to an exact Playground commit.
3. Before `openAt`, students cannot see the Release.
4. At or after `openAt`, current students of that teacher see it.
5. `dueAt` closes new submissions when `requiresSubmission` is true.

No scheduled job is needed to open a Release: visibility is computed from
current time when queried. Creation writes a `ChangeEvent` in the same database
transaction, with `availableAt` no earlier than `openAt`.

## 3. Install and open

1. Client immediately shows locally installed Playgrounds.
2. When online, it fetches public `bootstrap.json` and the signed-in user's open
   Releases, then merges them into the visible collection.
3. It compares local commit IDs with public heads and pinned Release commits.
4. It downloads only missing SHA-256 blobs, verifies them, then atomically
   installs the manifest.
5. It opens
   `app://pico/playgrounds/<playgroundId>/<commitId>/<entryPath>` in an isolated
   renderer.

Offline mode uses only installed commits and cached runtimes.

## 4. Edit and run

1. The renderer receives files through the narrow Client bridge.
2. Every edit updates `drafts/<userId>/<playgroundId>.json` locally.
3. The lesson runs Pyodide inside the isolated renderer.
4. Execution output stays local unless a future submission contract explicitly
   includes a bounded result artifact.

The host bridge transports files and events; execution stays inside the page.

## 5. Sync Work

1. Client sends editable `work/**`, current `baseCommitId`, and the installed
   `playgroundCommitId`.
2. Server verifies the student owns the Work and can access that Playground
   commit.
3. For an existing Work, Server requires `baseCommitId` to equal Work HEAD.
4. Server creates a commit and compare-and-swaps the Work ref.
5. Client records the returned commit ID, then marks the matching Draft clean.

If HEAD changed, Server returns `409` with `currentHeadCommitId`. Client keeps
the Draft and asks the user to reload, overwrite after confirmation, or merge
locally. Server never silently merges.

## 6. Restore history

Client selects a commit from Work history. Server verifies it belongs to that
Work, then creates a new commit with the selected tree and current HEAD as its
parent. History remains append-only and the restore itself can be undone.

## 7. Submit and review

1. Submit first synchronizes a dirty Draft.
2. Server creates an immutable Submission pinned to `releaseId` and the new or
   existing `workCommitId`.
3. For assignments, the Work metadata's `playgroundCommitId` must equal the
   Release's pinned Playground commit.
4. The same Work commit may be submitted more than once; each request creates a
   new `submissionId` unless its idempotency key is a retry.
5. Teacher reads that snapshot and posts immutable Comments. Authorized AI or
   system jobs use the same feedback object for reviews and learning guidance.

Submission and Comment creation each emit a `ChangeEvent` in the same database
transaction.

## 8. Pull changes

Client calls `GET /changes?after=<cursor>`, stores returned records by ID, and
atomically persists them with `nextCursor`. Replaying a page is safe. Events
announce durable facts; the Client fetches canonical resources as needed and
refreshes Releases on reconnect because the feed is not an access snapshot.

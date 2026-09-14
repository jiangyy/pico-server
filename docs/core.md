# Core model

## The two versioned objects

PicoPG has two Git-backed objects:

- **Playground**: publisher-owned lesson content.
- **Work**: one student's evolving work for one Playground.

Everything else is conventional application data.

## Git layout

Server owns one private bare repository, `data/picopg.git`.

```text
refs/heads/playgrounds/<playgroundId>
refs/heads/works/<playgroundId>/<studentId>
```

A Playground ref points to the latest published content commit. A Work ref
points to the student's current work commit.

The first Work commit has the selected Playground commit as its parent. This
makes the origin of the work part of the real Git graph. Later Work commits
form a normal linear history.

## Tree ownership

```text
course/**             publisher-owned built lesson files
starter/**            publisher-owned starter files
work/**               student-owned editable files
.picopg/work.json     server-owned Work metadata
```

`.picopg/work.json` contains:

```json
{
  "playgroundCommitId": "opaque-git-object-id"
}
```

Publisher requests may change only `course/**` and `starter/**`. Student sync
may change only `work/**`. Only Server writes `.picopg/**`.

## Git operations

- Publish: create a Playground commit and compare-and-swap its ref.
- Sync Work: create a Work commit and compare-and-swap its ref.
- Restore: create a new Work commit containing a historical Work tree; never
  move the ref backward.
- Upgrade course content: create a controlled merge commit whose first parent
  is current Work and second parent is the target Playground commit. Keep
  current `work/**`, replace publisher-owned paths, and update `work.json`.

Course upgrade is optional after v0.1. It is the only supported merge; Server
does not auto-merge concurrent student edits.

## Database objects

- `User`, `Session`, `TeacherStudent`
- `PlaygroundCatalog`
- `Release`
- `Submission`
- `Comment`
- `ChangeEvent`

`PlaygroundCatalog` stores policy, not content:

```text
playgroundId, access(public | release_only), archived
```

A Release pins one exact Playground commit and has `openAt`, optional `dueAt`,
and `requiresSubmission`. Releases are immutable; a correction creates another
Release with `supersedesReleaseId`.

A Submission pins `releaseId` and `workCommitId`. It never moves a Work ref.
A Comment is immutable feedback attached to a Submission. Its source is
`teacher`, `ai`, or `system`; teacher comments also record `authorUserId`.

A ChangeEvent is a durable notification hint with `availableAt`. Release events
become available at `max(createdAt, openAt)`; other events use `createdAt`.
Canonical rows remain the authority.

In v0.1 a teacher's Release applies to that teacher's current
`TeacherStudent` relationships. Recipient snapshots are added only if the
product later requires historical class membership.

## Sources of truth

| Fact | Authority |
| --- | --- |
| Playground and Work contents/history | Git objects and refs |
| Access, relationships, releases, submissions, comments | database |
| Commit manifests and public `bootstrap.json` | rebuildable cache |
| Installed content, blobs, drafts, cursor | Client local storage |
| Runtime and EXE packages | immutable artifact storage |

## Invariants

1. A public API commit ID is opaque; never assume SHA-1 length or syntax.
2. Every Release and Submission pins an immutable commit.
3. Sync succeeds only when `baseCommitId` equals current Work HEAD.
4. A dirty Draft is never discarded after a conflict or failed sync.
5. `release_only` content requires authorization even if its commit ID leaks.
6. Teacher reads only submissions made under that teacher's Releases; live
   student Work is private by default.
7. Admin manages policy and identities but does not read student Work by
   default.
8. Git holds durable source snapshots, not application workflow state.

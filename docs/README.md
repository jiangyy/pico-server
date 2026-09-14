# PicoPG contracts

These documents define the contract shared by Server, Client, Admin, and the
`learn-python` publisher.

Read them in this order:

1. [core.md](core.md) — authoritative model and invariants
2. [flows.md](flows.md) — end-to-end behavior
3. [api-conventions.md](api-conventions.md) — HTTP rules
4. [server.md](server.md) — persistence and deployment
5. [client.md](client.md) — local-first Client behavior
6. [account.md](account.md) — identity and authorization

`core.md` owns terminology. Other files describe projections of that model and
must not redefine it.

## Scope

PicoPG is a local-first Python learning system:

- `learn-python` is the only source of Playground content.
- Server distributes immutable content, records learning history, and handles
  collaboration.
- Web and desktop Client share one frontend application layer. Host adapters
  provide storage and runtime capabilities.
- Client downloads lessons, edits and runs Python locally, and synchronizes on
  explicit submission or user-requested sync.
- Teacher and Admin are ordinary role-based views over the same Server API.

## Deliberate non-goals

- Clients do not clone or push Git repositories.
- Git does not manage accounts, permissions, releases, submissions, comments,
  notifications, runtime binaries, or transient execution output.
- Server does not silently merge conflicting student edits.
- v0.1 does not require one physical Git repository per student or Playground.

## Authority

When documents disagree, use this order:

1. `core.md`
2. the relevant contract in this directory
3. implementation comments and older notes

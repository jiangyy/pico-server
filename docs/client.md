# Web and Client contract

## Shared frontend

Web and desktop Client share routes, components, state transitions, API types,
and conflict behavior. Platform differences live behind one small host adapter:

- Web uses browser storage and browser Pyodide loading.
- Desktop uses the file-backed cache and isolated host bridge below.

Do not scatter platform checks through feature components. Desktop guarantees
offline-installed use; Web provides the same guarantee only after its browser
storage has completed installation.

## Local-first behavior

Client opens without a network connection and immediately lists installed
Playgrounds. Network refresh is additive: failed requests do not hide installed
content or discard Drafts.

Suggested logical storage:

```text
content/blobs/<sha256>
content/manifests/<playgroundId>/<commitId>.json
runtimes/<runtimeVersion>/...
drafts/<userId>/<playgroundId>.json
state/<userId>.json
```

The exact physical layout is private to Client. Drafts are namespaced by user
so switching accounts cannot expose or overwrite another student's edits.

## Download and install

1. Fetch public bootstrap and the signed-in user's open Releases, then merge
   them into one visible collection.
2. Compare their commit IDs with installed manifests.
3. Download missing blobs and runtimes.
4. Verify size and SHA-256 before admitting each blob to the cache.
5. Write the new manifest to a temporary file and rename it into place.

An interrupted install leaves the previous manifest usable. Cache cleanup may
remove only blobs not referenced by an installed manifest or Draft.

## Renderer boundary

Playground content opens at:

```text
app://pico/playgrounds/<playgroundId>/<commitId>/<entryPath>
```

Use an isolated renderer with no Node.js access and a restrictive Content
Security Policy. The bridge exposes only:

```text
files.load()
files.save(draft)
submit()
on(eventName, handler)
```

Validate every message on both sides. The page owns Pyodide execution; the host
does not expose a general command channel.

## Draft and sync state

A Draft records its base Work commit, Playground commit, editable files, dirty
state, and last local save time. Local save errors are visible and block any UI
claim that work is safely saved.

Sync sends one complete bounded `work/**` snapshot. On success, Client updates
the local head before marking the matching Draft clean. If the Draft changed
during the request, it remains dirty.

On `409 work_head_changed`, keep the Draft and show the current server head.
User choice controls reload, overwrite, or local merge. Overwrite is a new
explicit sync against the latest base, not a force-push.

## Submission

Submit is a two-step user action:

1. synchronize a dirty Draft;
2. create a Submission pinned to the resulting Work commit.

If either step fails, show which step failed. Retrying Submission uses the same
idempotency key until the outcome is known.

## Change feed

For each user, persist change items and `nextCursor` atomically. Store items by
stable ID so page replay is harmless. Change events trigger fetching canonical
Release, Submission, or Comment data; they are not a second copy of it. Teacher,
AI, and system feedback all arrive as Comments with an explicit source.

## Credentials

The main process stores session tokens with the operating system credential
facility, such as Electron `safeStorage`. Tokens and authenticated response
bodies are never exposed to untrusted Playground content.

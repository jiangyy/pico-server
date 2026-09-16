"""Rebuildable public caches.

Everything under the injected generated directory is disposable.  The caches
are written only from the authoritative sources: the private Git repository,
the SQLite database, and the immutable artifacts directory.  They are caches,
never authority: an access-control decision is always made against the live
database and repository, so a stale, forged, or deleted cache can neither
grant nor remove access to private content.

Each file is written by building the complete payload first and then replacing
the target with a sibling temporary file, so a failure before the replacement
leaves the previous valid output untouched and a rebuild after deleting the
directory restores every file.
"""

import json
import os
import tempfile
from pathlib import Path

import artifacts
import manifests
import playgrounds


__all__ = [
    "ARTIFACT_INDEX_FILENAME",
    "BOOTSTRAP_FILENAME",
    "CATALOG_FILENAME",
    "GENERATED_FILENAMES",
    "MANIFESTS_FILENAME",
    "atomic_write",
    "build_payloads",
    "public_catalog",
    "rebuild",
]


ARTIFACT_INDEX_FILENAME = "artifact-index.json"
BOOTSTRAP_FILENAME = "bootstrap.json"
CATALOG_FILENAME = "catalog.json"
MANIFESTS_FILENAME = "manifests.json"

# Replacement order: the files that other caches summarize are written first.
GENERATED_FILENAMES = (
    ARTIFACT_INDEX_FILENAME,
    CATALOG_FILENAME,
    MANIFESTS_FILENAME,
    BOOTSTRAP_FILENAME,
)


def _encode(payload):
    """Return deterministic UTF-8 JSON bytes for ``payload``."""

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def atomic_write(path, data):
    """Write ``data`` to ``path`` through a sibling temporary file.

    The temporary file is created in the destination directory, flushed and
    fsynced, then moved into place with :func:`os.replace`.  A failure before
    the replacement removes the temporary file and leaves ``path`` unchanged.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def public_catalog(connection, repository):
    """Return the public, unarchived Playground catalog from authority.

    Only public Playgrounds are listed, and each entry records the exact
    current commit.  Private metadata and Release visibility are deliberately
    absent, because a generated cache must never carry per-actor access.
    """

    rows = connection.execute(
        "SELECT id FROM playgrounds"
        " WHERE policy = ? AND archived_at IS NULL"
        " ORDER BY id",
        (playgrounds.PUBLIC_POLICY,),
    ).fetchall()

    catalog = []
    for row in rows:
        commit_id = manifests.current_playground_commit(repository, row["id"])
        if commit_id is None:
            continue
        catalog.append(
            {"playgroundId": row["id"], "commitId": commit_id}
        )
    return catalog


def _build_manifests(connection, repository, catalog):
    """Return the deterministic manifests of every public current commit."""

    manifest_index = []
    for item in catalog:
        manifest = manifests.generate_manifest(
            connection,
            None,
            repository,
            item["playgroundId"],
            item["commitId"],
        )
        mapping = manifests.manifest_to_mapping(manifest)
        mapping["playgroundId"] = item["playgroundId"]
        manifest_index.append(mapping)
    return manifest_index


def _previous_entries(generated_path):
    """Return the previously published entries, or ``None`` when unusable.

    A deleted or corrupt cache is not an error: it is treated as absent so the
    next rebuild recreates it from the authoritative artifact directory.
    """

    path = Path(generated_path) / ARTIFACT_INDEX_FILENAME
    if not path.is_file():
        return None
    try:
        mapping = json.loads(path.read_text(encoding="utf-8"))
        return artifacts.mapping_to_index(mapping)
    except (OSError, ValueError, TypeError, KeyError, artifacts.ArtifactError):
        return None


def build_payloads(generated_path, *, connection, repository, artifacts_path):
    """Build every generated payload without touching the output directory.

    Raises before any file is replaced when an authoritative source cannot
    produce a valid payload, so a rebuild failure preserves the old output.
    """

    previous = _previous_entries(generated_path)
    entries = artifacts.index_artifacts(artifacts_path, previous)
    catalog = public_catalog(connection, repository)
    manifest_index = _build_manifests(connection, repository, catalog)

    artifact_mappings = [entry.to_mapping() for entry in entries]
    return {
        ARTIFACT_INDEX_FILENAME: _encode({"artifacts": artifact_mappings}),
        CATALOG_FILENAME: _encode({"catalog": catalog}),
        MANIFESTS_FILENAME: _encode({"manifests": manifest_index}),
        BOOTSTRAP_FILENAME: _encode(
            {"artifacts": artifact_mappings, "catalog": catalog}
        ),
    }


def rebuild(generated_path, *, connection, repository, artifacts_path):
    """Rebuild all generated caches and return the written payloads."""

    generated_path = Path(generated_path)
    payloads = build_payloads(
        generated_path,
        connection=connection,
        repository=repository,
        artifacts_path=artifacts_path,
    )
    for filename in GENERATED_FILENAMES:
        atomic_write(generated_path / filename, payloads[filename])
    return payloads

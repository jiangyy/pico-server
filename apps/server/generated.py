"""Rebuildable public caches published as atomic generations.

Everything under the injected generated directory is disposable.  The caches
are built only from the authoritative sources: the private Git repository, the
SQLite database, and the immutable artifacts directory.  They are derived
copies, never authority: every read of a commit is generated from Git, so a
stale, forged, or deleted cache can neither publish nor hide a commit.

On disk a completed cache is exactly::

    generated/
      current.json
      generations/
        <generation-id>/
          bootstrap.json
          artifact-index.json

A generation id is the lowercase SHA-256 of the exact canonical bytes of
``bootstrap.json``, one newline byte, and ``artifact-index.json``, in that
order.  ``current.json`` is canonical JSON of ``{"generation": "<id>"}`` and is
replaced atomically only after the generation directory is complete and
durable.  A reader resolves ``current.json`` once and reads both files from
that one generation, so it can never mix generations.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import artifacts
import manifests
import permissions


__all__ = [
    "ARTIFACT_INDEX_FILENAME",
    "BOOTSTRAP_FILENAME",
    "CATALOG_FILENAME",
    "CURRENT_FILENAME",
    "GENERATED_FILENAMES",
    "GENERATION_FILENAMES",
    "GENERATIONS_DIRNAME",
    "MANIFESTS_FILENAME",
    "GeneratedCacheError",
    "Generation",
    "atomic_write",
    "build_payloads",
    "generation_id",
    "public_catalog",
    "rebuild",
    "resolve_current_generation",
]


ARTIFACT_INDEX_FILENAME = "artifact-index.json"
BOOTSTRAP_FILENAME = "bootstrap.json"
CATALOG_FILENAME = "catalog.json"
MANIFESTS_FILENAME = "manifests.json"

CURRENT_FILENAME = "current.json"
GENERATIONS_DIRNAME = "generations"

# The exact files one generation directory contains, in generation-id order.
GENERATION_FILENAMES = (BOOTSTRAP_FILENAME, ARTIFACT_INDEX_FILENAME)

# The top-level entries a completed generated directory exposes.
GENERATED_FILENAMES = (CURRENT_FILENAME, GENERATIONS_DIRNAME)

_GENERATION_ID_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")


class GeneratedCacheError(RuntimeError):
    """The generated cache is missing, corrupt, or cannot be published."""


@dataclass(frozen=True)
class Generation:
    """One validated current generation: its id and exact payload bytes."""

    generation_id: str
    bootstrap: bytes
    artifact_index: bytes


def _encode(payload):
    """Return canonical UTF-8 JSON bytes with one trailing newline."""

    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def generation_id(bootstrap_bytes, artifact_index_bytes):
    """Return the lowercase SHA-256 id for one generation's exact bytes."""

    hasher = hashlib.sha256()
    hasher.update(bootstrap_bytes)
    hasher.update(b"\n")
    hasher.update(artifact_index_bytes)
    return hasher.hexdigest()


def atomic_write(path, data):
    """Write ``data`` to ``path`` through a sibling temporary file.

    The temporary file is created in the destination directory, flushed and
    fsynced, then moved into place with :func:`os.replace`.  A failure before
    the replacement removes the temporary file and leaves ``path`` unchanged.
    """

    path = Path(path)
    permissions.ensure_private_directory(path.parent)

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
    """Return the published Playground catalog from authority.

    Every stored Playground is readable by every client, and each entry records
    the exact current commit, so the cache holds no per-caller access.
    """

    rows = connection.execute(
        "SELECT id FROM playgrounds ORDER BY id"
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


def _build_manifests(repository, catalog):
    """Return the deterministic manifests of every published commit."""

    manifest_index = []
    for item in catalog:
        manifest = manifests.generate_manifest(
            repository,
            item["playgroundId"],
            item["commitId"],
        )
        mapping = manifests.manifest_to_mapping(manifest)
        mapping["playgroundId"] = item["playgroundId"]
        manifest_index.append(mapping)
    return manifest_index


def _previous_entries(generated_path):
    """Return the current generation's entries, or ``None`` when unusable.

    A deleted or corrupt cache is not an error: it is treated as absent so the
    next rebuild recreates it from the authoritative artifact directory.
    """

    try:
        generation = resolve_current_generation(generated_path)
    except GeneratedCacheError:
        return None
    try:
        mapping = json.loads(generation.artifact_index.decode("utf-8"))
        return artifacts.mapping_to_index(mapping)
    except (ValueError, TypeError, KeyError, artifacts.ArtifactError):
        return None


def build_payloads(generated_path, *, connection, repository, artifacts_path):
    """Build every generated payload without touching the output directory.

    Raises before any file is replaced when an authoritative source cannot
    produce a valid payload, so a rebuild failure preserves the old output.
    """

    previous = _previous_entries(generated_path)
    entries = artifacts.index_artifacts(artifacts_path, previous)
    catalog = public_catalog(connection, repository)
    manifest_index = _build_manifests(repository, catalog)

    artifact_mappings = artifacts.index_to_mapping(entries)["artifacts"]
    return {
        ARTIFACT_INDEX_FILENAME: _encode({"artifacts": artifact_mappings}),
        CATALOG_FILENAME: _encode({"catalog": catalog}),
        MANIFESTS_FILENAME: _encode({"manifests": manifest_index}),
        BOOTSTRAP_FILENAME: _encode(
            {"artifacts": artifact_mappings, "catalog": catalog}
        ),
    }


def _fsync_directory(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_generation_file(path, data):
    with open(path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _discard_temporary(path, generations_root):
    resolved = os.path.realpath(path)
    if os.path.dirname(resolved) != generations_root:
        return
    shutil.rmtree(resolved, ignore_errors=True)


def _read_generation_directory(directory, expected_id):
    """Validate and return ``(bootstrap, artifact_index)`` for one generation."""

    if directory.is_symlink() or not directory.is_dir():
        raise GeneratedCacheError("generation directory is missing")
    try:
        names = {entry.name for entry in os.scandir(directory)}
    except OSError as error:
        raise GeneratedCacheError(
            "generation directory is unreadable"
        ) from error
    if names != set(GENERATION_FILENAMES):
        raise GeneratedCacheError(
            "generation directory has unexpected entries"
        )

    payload = {}
    for name in GENERATION_FILENAMES:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise GeneratedCacheError(
                "generation entry is not a regular file"
            )
        try:
            payload[name] = path.read_bytes()
        except OSError as error:
            raise GeneratedCacheError(
                "generation entry is unreadable"
            ) from error

    bootstrap = payload[BOOTSTRAP_FILENAME]
    artifact_index = payload[ARTIFACT_INDEX_FILENAME]
    if generation_id(bootstrap, artifact_index) != expected_id:
        raise GeneratedCacheError(
            "generation id does not match its content"
        )

    # A truncated or otherwise unparsable payload is rejected even if its
    # bytes happen to hash to the directory name.
    for data in (bootstrap, artifact_index):
        try:
            json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise GeneratedCacheError(
                "generation payload is not valid JSON"
            ) from error
    return bootstrap, artifact_index


def resolve_current_generation(generated_path):
    """Resolve ``current.json`` once and return the validated generation.

    Raises :class:`GeneratedCacheError` for a missing, corrupt, or mismatched
    pointer, so a caller never serves bytes from two generations or from an
    unvalidated directory.
    """

    generated_path = Path(generated_path)
    current = generated_path / CURRENT_FILENAME
    if current.is_symlink() or not current.is_file():
        raise GeneratedCacheError("current pointer is missing")
    try:
        document = json.loads(current.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise GeneratedCacheError("current pointer is not valid JSON") from error
    if type(document) is not dict or set(document) != {"generation"}:
        raise GeneratedCacheError("current pointer has an unexpected shape")
    identifier = document["generation"]
    if (
        type(identifier) is not str
        or _GENERATION_ID_PATTERN.match(identifier) is None
    ):
        raise GeneratedCacheError("current pointer id is not valid")

    directory = generated_path / GENERATIONS_DIRNAME / identifier
    bootstrap, artifact_index = _read_generation_directory(
        directory, identifier
    )
    return Generation(
        generation_id=identifier,
        bootstrap=bootstrap,
        artifact_index=artifact_index,
    )


def rebuild(generated_path, *, connection, repository, artifacts_path):
    """Rebuild the generated caches and publish one complete generation.

    Authority is read first; only after every payload is built is a new
    generation directory written and fsynced, and only after that is
    ``current.json`` replaced.  A failure leaves the previous generation
    current and removes any task-owned temporary directory.
    """

    generated_path = Path(generated_path)
    payloads = build_payloads(
        generated_path,
        connection=connection,
        repository=repository,
        artifacts_path=artifacts_path,
    )
    _publish_generation(generated_path, payloads)
    return payloads


def _publish_generation(generated_path, payloads):
    bootstrap = payloads[BOOTSTRAP_FILENAME]
    artifact_index = payloads[ARTIFACT_INDEX_FILENAME]
    identifier = generation_id(bootstrap, artifact_index)

    generations = generated_path / GENERATIONS_DIRNAME
    permissions.ensure_private_directory(generations)
    generations_root = os.path.realpath(generations)

    final = generations / identifier
    if final.is_symlink():
        raise GeneratedCacheError("generation path is a symbolic link")
    if final.is_dir():
        _read_generation_directory(final, identifier)
    else:
        temporary = Path(
            tempfile.mkdtemp(
                dir=generations, prefix=f".{identifier}.", suffix=".tmp"
            )
        )
        try:
            _write_generation_file(
                temporary / BOOTSTRAP_FILENAME, bootstrap
            )
            _write_generation_file(
                temporary / ARTIFACT_INDEX_FILENAME, artifact_index
            )
            _fsync_directory(temporary)
            if final.exists():
                _read_generation_directory(final, identifier)
                _discard_temporary(temporary, generations_root)
            else:
                os.rename(temporary, final)
                _fsync_directory(generations)
        except BaseException:
            _discard_temporary(temporary, generations_root)
            raise

    atomic_write(
        generated_path / CURRENT_FILENAME,
        _encode({"generation": identifier}),
    )

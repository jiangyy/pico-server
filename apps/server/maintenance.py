"""Offline backup and restore tooling for one PicoPG data directory.

A PicoPG data directory holds three authoritative sources and one disposable
cache::

    <data_dir>/app.db          SQLite database
    <data_dir>/picopg.git      complete bare Git repository
    <data_dir>/artifacts/      immutable runtime artifacts
    <data_dir>/generated/      rebuildable public caches (never backed up)

The tooling in this module is explicitly offline.  A live service can mutate
the database, the repository, and the artifacts while they are being read, so
every write operation requires the caller to confirm the service is stopped by
passing ``service_stopped=True``.  Without that flag the operation raises
:class:`MaintenanceError` with :data:`REASON_SERVICE_RUNNING`.

``create_backup`` builds the recovery set in a new temporary sibling directory,
records a manifest of every backed-up file (relative path, size and SHA-256),
fsyncs what it can, and only then atomically renames the directory into place.
A failure at any point removes the temporary directory and leaves no completed
backup; an existing destination is never overwritten.

``validate_backup`` verifies the manifest against the files on disk, opens the
SQLite copy read-only, checks the Git repository with a full ``git fsck``
integrity pass, and makes sure every commit id recorded by the database still
resolves inside the backed up repository.  ``restore_backup`` first validates
a backup and then copies the verified files into a brand new, empty data
directory; it never deletes or replaces a live data directory.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import artifacts
import bundles
import permissions
import repository as repository_module
import works
import database


__all__ = [
    "MaintenanceError",
    "REASON_ALTERNATES_PRESENT",
    "REASON_COMMIT_MISSING",
    "REASON_DATA_DIR_INVALID",
    "REASON_DATABASE_INVALID",
    "REASON_DESTINATION_EXISTS",
    "REASON_DESTINATION_INVALID",
    "REASON_FILE_CHANGED",
    "REASON_FILE_MISSING",
    "REASON_GIT_INVALID",
    "REASON_MANIFEST_INVALID",
    "REASON_PATH_ESCAPE",
    "REASON_RESTORE_TARGET_INVALID",
    "REASON_SERVICE_RUNNING",
    "REASON_UNEXPECTED_FILE",
    "create_backup",
    "compact_repository",
    "restore_backup",
    "validate_backup",
    "validate_data_dir",
]


# Names inside one data directory.  They mirror the defaults used by the
# server so a data directory produced by :func:`server.make_server` is exactly
# the layout this module expects.
DATABASE_FILENAME = "app.db"
REPOSITORY_DIRNAME = "picopg.git"
ARTIFACTS_DIRNAME = "artifacts"

MANIFEST_FILENAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 1

_CHUNK_BYTES = 1024 * 1024
_MAX_MAINTENANCE_COMMITS = 100_000

# Stable reason codes.  The message carries the human-readable detail, while
# the reason stays machine readable for callers and tests.
REASON_ALTERNATES_PRESENT = "alternates_present"
REASON_COMMIT_MISSING = "commit_missing"
REASON_DATA_DIR_INVALID = "data_dir_invalid"
REASON_DATABASE_INVALID = "database_invalid"
REASON_DESTINATION_EXISTS = "destination_exists"
REASON_DESTINATION_INVALID = "destination_invalid"
REASON_FILE_CHANGED = "file_changed"
REASON_FILE_MISSING = "file_missing"
REASON_GIT_INVALID = "git_invalid"
REASON_MANIFEST_INVALID = "manifest_invalid"
REASON_PATH_ESCAPE = "path_escape"
REASON_RESTORE_TARGET_INVALID = "restore_target_invalid"
REASON_SERVICE_RUNNING = "service_running"
REASON_UNEXPECTED_FILE = "unexpected_file"


class MaintenanceError(Exception):
    """Raised when an offline backup operation violates its contract."""

    def __init__(self, reason, message=None):
        self.reason = reason
        super().__init__(message or reason)


def _require_service_stopped(service_stopped):
    """Enforce the explicit stopped-service contract.

    The flag is intentionally keyword-only and must be the literal ``True`` so
    a caller cannot accidentally back up a running service.
    """

    if service_stopped is not True:
        raise MaintenanceError(
            REASON_SERVICE_RUNNING,
            "the PicoPG service must be stopped before an offline backup; "
            "stop it and call again with service_stopped=True",
        )


def _is_safe_relative_path(path):
    """Return whether ``path`` is one safe, portable, relative path."""

    if type(path) is not str or not path:
        return False
    if "\\" in path or path.startswith("/"):
        return False
    if any(ord(character) < 32 or character == "\x7f" for character in path):
        return False
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return False
    return True


def _contained(root, candidate):
    """Return whether the resolved ``candidate`` stays inside ``root``."""

    resolved_root = os.path.realpath(root)
    resolved_candidate = os.path.realpath(candidate)
    if resolved_candidate == resolved_root:
        return True
    return resolved_candidate.startswith(resolved_root + os.sep)


def _sha256_file(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _fsync_file(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path):
    """fsync one directory, ignoring platforms where this is unsupported."""

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


def _fsync_tree(root):
    for current, directories, _ in os.walk(root, topdown=False):
        for name in directories:
            _fsync_directory(os.path.join(current, name))
        _fsync_directory(current)


# --- copying authoritative sources ------------------------------------------


def _copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(source, "rb") as reader, open(target, "wb") as writer:
        for chunk in iter(lambda: reader.read(_CHUNK_BYTES), b""):
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    os.chmod(target, os.stat(source).st_mode & 0o777)


def _copy_tree(source, target):
    """Copy one directory tree without ever following a symbolic link."""

    if source.is_symlink():
        raise MaintenanceError(
            REASON_PATH_ESCAPE, f"refusing to copy symlink {source}"
        )
    if not source.is_dir():
        raise MaintenanceError(
            REASON_DATA_DIR_INVALID, f"source directory is missing: {source}"
        )

    target.mkdir(parents=True, exist_ok=True)
    for entry in sorted(os.scandir(source), key=lambda item: item.name):
        source_path = Path(entry.path)
        target_path = target / entry.name
        if entry.is_symlink():
            raise MaintenanceError(
                REASON_PATH_ESCAPE, "refusing to copy a symbolic link"
            )
        if entry.is_dir(follow_symlinks=False):
            _copy_tree(source_path, target_path)
        elif entry.is_file(follow_symlinks=False):
            _copy_file(source_path, target_path)
        else:
            raise MaintenanceError(
                REASON_UNEXPECTED_FILE, "source contains a special file"
            )


def _copy_database(source, target):
    """Copy the SQLite database with the online backup API.

    The backup API produces a single transactionally consistent database file
    even when the source has a hot journal, and it never copies a ``-wal`` or
    ``-shm`` sidecar into the recovery set.
    """

    if source.is_symlink() or not source.is_file():
        raise MaintenanceError(
            REASON_DATA_DIR_INVALID, "database file is missing"
        )
    target.parent.mkdir(parents=True, exist_ok=True)

    uri = Path(source).resolve().as_uri() + "?mode=ro"
    try:
        source_connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise MaintenanceError(
            REASON_DATABASE_INVALID, "could not open the database for backup"
        ) from error
    try:
        source_connection.execute("PRAGMA query_only = ON")
        target_connection = sqlite3.connect(target)
        try:
            source_connection.backup(target_connection)
            target_connection.commit()
        finally:
            target_connection.close()
    except sqlite3.Error as error:
        raise MaintenanceError(
            REASON_DATABASE_INVALID, "could not copy the database"
        ) from error
    finally:
        source_connection.close()
    _fsync_file(target)


def _copy_repository(source, target):
    """Copy a complete bare repository, refusing a shared object store."""

    alternates = source / "objects" / "info" / "alternates"
    if alternates.is_file() and alternates.stat().st_size > 0:
        raise MaintenanceError(
            REASON_ALTERNATES_PRESENT,
            "the repository uses object alternates and is not self-contained",
        )
    _copy_tree(source, target)


# --- manifest ---------------------------------------------------------------


def _build_manifest(root):
    """Return ordered manifest entries for every file below ``root``."""

    resolved_root = os.path.realpath(root)
    entries = []
    for current, directories, files in os.walk(root, followlinks=False):
        for name in list(directories):
            if os.path.islink(os.path.join(current, name)):
                raise MaintenanceError(
                    REASON_PATH_ESCAPE, "backup contains a symbolic link"
                )
        for name in sorted(files):
            path = os.path.join(current, name)
            if os.path.islink(path):
                raise MaintenanceError(
                    REASON_PATH_ESCAPE, "backup contains a symbolic link"
                )
            if not os.path.isfile(path):
                raise MaintenanceError(
                    REASON_UNEXPECTED_FILE, "backup contains a special file"
                )
            if not _contained(resolved_root, path):
                raise MaintenanceError(
                    REASON_PATH_ESCAPE, "backup path escapes its root"
                )
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if (
                os.path.realpath(current) == resolved_root
                and relative == MANIFEST_FILENAME
            ):
                continue
            if not _is_safe_relative_path(relative):
                raise MaintenanceError(
                    REASON_PATH_ESCAPE, "backup path is not portable"
                )
            entries.append(
                {
                    "path": relative,
                    "size": os.path.getsize(path),
                    "sha256": _sha256_file(path),
                }
            )
    entries.sort(key=lambda entry: entry["path"])
    return entries


def _write_manifest(root, entries):
    mapping = {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "entries": entries,
    }
    data = json.dumps(
        mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    target = Path(root) / MANIFEST_FILENAME
    temporary = target.with_name(target.name + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    _fsync_file(target)


def _parse_manifest(mapping):
    """Validate and normalize a manifest mapping into a list of entries."""

    if not isinstance(mapping, dict):
        raise MaintenanceError(
            REASON_MANIFEST_INVALID, "manifest must be a JSON object"
        )
    if mapping.get("schemaVersion") != MANIFEST_SCHEMA_VERSION:
        raise MaintenanceError(
            REASON_MANIFEST_INVALID, "unsupported manifest schema version"
        )
    raw_entries = mapping.get("entries")
    if not isinstance(raw_entries, list):
        raise MaintenanceError(
            REASON_MANIFEST_INVALID, "manifest entries must be a list"
        )

    entries = []
    seen = set()
    for item in raw_entries:
        if not isinstance(item, dict):
            raise MaintenanceError(
                REASON_MANIFEST_INVALID, "manifest entry must be an object"
            )
        path = item.get("path")
        size = item.get("size")
        sha256 = item.get("sha256")
        if not _is_safe_relative_path(path) or path == MANIFEST_FILENAME:
            raise MaintenanceError(
                REASON_MANIFEST_INVALID, "manifest path is not allowed"
            )
        if type(size) is not int or size < 0:
            raise MaintenanceError(
                REASON_MANIFEST_INVALID, "manifest size is not valid"
            )
        if (
            type(sha256) is not str
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise MaintenanceError(
                REASON_MANIFEST_INVALID, "manifest hash is not valid"
            )
        if path in seen:
            raise MaintenanceError(
                REASON_MANIFEST_INVALID, "manifest path is duplicated"
            )
        seen.add(path)
        entries.append({"path": path, "size": size, "sha256": sha256})
    return entries


def _collect_backup_files(backup):
    """Return the relative files below ``backup`` except the manifest."""

    root = Path(backup)
    collected = set()
    for current, directories, files in os.walk(root, followlinks=False):
        for name in list(directories):
            if os.path.islink(os.path.join(current, name)):
                raise MaintenanceError(
                    REASON_PATH_ESCAPE, "backup contains a symbolic link"
                )
        for name in files:
            path = os.path.join(current, name)
            if os.path.islink(path):
                raise MaintenanceError(
                    REASON_PATH_ESCAPE, "backup contains a symbolic link"
                )
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if relative == MANIFEST_FILENAME:
                continue
            if not _is_safe_relative_path(relative):
                raise MaintenanceError(
                    REASON_PATH_ESCAPE, "backup path is not portable"
                )
            collected.add(relative)
    return collected


def _verify_files(backup, entries):
    """Check sizes and hashes and that no extra or missing file exists."""

    resolved_root = os.path.realpath(backup)
    expected = {entry["path"]: entry for entry in entries}
    actual = _collect_backup_files(backup)

    for path in sorted(set(expected) - actual):
        raise MaintenanceError(
            REASON_FILE_MISSING, "a manifest file is missing from the backup"
        )
    for path in sorted(actual - set(expected)):
        raise MaintenanceError(
            REASON_UNEXPECTED_FILE, "the backup contains an unlisted file"
        )

    for path, entry in expected.items():
        target = Path(backup) / path
        if target.is_symlink() or not _contained(resolved_root, target):
            raise MaintenanceError(
                REASON_PATH_ESCAPE, "backup path escapes its root"
            )
        if target.stat().st_size != entry["size"]:
            raise MaintenanceError(
                REASON_FILE_CHANGED, "a backed-up file changed size"
            )
        if _sha256_file(target) != entry["sha256"]:
            raise MaintenanceError(
                REASON_FILE_CHANGED, "a backed-up file changed contents"
            )


# --- validation -------------------------------------------------------------


def _open_read_only(path):
    if path.is_symlink() or not path.is_file():
        raise MaintenanceError(
            REASON_FILE_MISSING, "the backed-up database is missing"
        )
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise MaintenanceError(
            REASON_DATABASE_INVALID, "could not open the database read-only"
        ) from error


def _validate_database(connection):
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as error:
        raise MaintenanceError(
            REASON_DATABASE_INVALID, "database integrity check failed"
        ) from error
    if row is None or row[0] != "ok":
        raise MaintenanceError(
            REASON_DATABASE_INVALID, "database integrity check failed"
        )

    try:
        problems = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as error:
        raise MaintenanceError(
            REASON_DATABASE_INVALID, "database foreign keys are invalid"
        ) from error
    if problems:
        raise MaintenanceError(
            REASON_DATABASE_INVALID, "database foreign keys are invalid"
        )

    # A backup that would not start Server v0.1 is not a valid backup: a
    # forged migration history or a missing business table must fail here.
    try:
        database.validate_schema(connection)
    except database.SchemaValidationError as error:
        raise MaintenanceError(
            REASON_DATABASE_INVALID, "database schema is invalid"
        ) from error


def _validate_repository(path):
    if path.is_symlink() or not path.is_dir():
        raise MaintenanceError(
            REASON_GIT_INVALID, "the backed-up repository is missing"
        )
    alternates = path / "objects" / "info" / "alternates"
    if alternates.is_file() and alternates.stat().st_size > 0:
        raise MaintenanceError(
            REASON_ALTERNATES_PRESENT,
            "the repository uses object alternates and is not self-contained",
        )

    try:
        repository = repository_module.open_repository(path)
    except repository_module.RepositoryError as error:
        raise MaintenanceError(
            REASON_GIT_INVALID, "the backup is not a valid bare repository"
        ) from error

    try:
        # A full ``git fsck`` is required here: ``--connectivity-only`` never
        # reads blobs and therefore cannot detect corruption in the object
        # store.  ``fsck`` is read-only and non-interactive.
        completed = repository_module._run_git(
            ("fsck", "--full", "--strict", "--no-progress"),
            cwd=repository.path,
        )
    except repository_module.RepositoryError as error:
        raise MaintenanceError(
            REASON_GIT_INVALID, "could not check the repository integrity"
        ) from error
    if completed.returncode != 0:
        raise MaintenanceError(
            REASON_GIT_INVALID, "the repository failed its integrity check"
        )
    return repository


def _require_commit(repository, commit_id):
    """Return the resolved commit id, or raise a stable maintenance error."""

    try:
        resolved = repository_module.resolve_commit(repository, commit_id)
    except repository_module.RepositoryError as error:
        raise MaintenanceError(
            REASON_COMMIT_MISSING,
            "a database commit reference is not a Git object id",
        ) from error
    if resolved is None:
        raise MaintenanceError(
            REASON_COMMIT_MISSING,
            "a database commit reference does not exist in the repository",
        )
    return resolved


def _require_ancestor(repository, commit_id, ref):
    """Require ``commit_id`` to be reachable from ``ref``."""

    _require_commit(repository, commit_id)
    try:
        completed = repository_module._run_git(
            ("merge-base", "--is-ancestor", commit_id, ref),
            cwd=repository.path,
        )
    except repository_module.RepositoryError as error:
        raise MaintenanceError(
            REASON_GIT_INVALID, "could not check commit ancestry"
        ) from error
    if completed.returncode != 0:
        raise MaintenanceError(
            REASON_COMMIT_MISSING,
            "a database commit is not part of its required history",
        )


def _work_metadata_pin(repository, work_commit_id):
    """Return the Playground commit pinned by a Work commit's metadata."""

    try:
        return works._read_base_work_playground_commit_id(
            repository, work_commit_id
        )
    except (works.WorkError, repository_module.RepositoryError) as error:
        raise MaintenanceError(
            REASON_COMMIT_MISSING, "Work metadata is missing or invalid"
        ) from error


def _validate_commit_references(connection, repository):
    """Validate database-to-Git relationships, not object existence alone."""

    releases = connection.execute(
        "SELECT id, playground_id, playground_commit_id FROM releases"
    ).fetchall()
    release_by_id = {}
    for row in releases:
        release_id, playground_id, playground_commit_id = row[0], row[1], row[2]
        release_by_id[release_id] = (playground_id, playground_commit_id)
        try:
            ref = repository.playground_ref(playground_id)
        except repository_module.RepositoryError as error:
            raise MaintenanceError(
                REASON_COMMIT_MISSING,
                "a Release names an invalid Playground identifier",
            ) from error
        _require_ancestor(repository, playground_commit_id, ref)

    submissions = connection.execute(
        "SELECT id, release_id, student_id, work_commit_id FROM submissions"
    ).fetchall()
    for row in submissions:
        release_id, student_id, work_commit_id = row[1], row[2], row[3]
        if release_id not in release_by_id:
            raise MaintenanceError(
                REASON_COMMIT_MISSING,
                "a Submission references a missing Release",
            )
        playground_id, playground_commit_id = release_by_id[release_id]
        try:
            ref = repository.work_ref(playground_id, student_id)
        except repository_module.RepositoryError as error:
            raise MaintenanceError(
                REASON_COMMIT_MISSING,
                "a Submission names an invalid Work identifier",
            ) from error
        _require_ancestor(repository, work_commit_id, ref)
        if _work_metadata_pin(repository, work_commit_id) != playground_commit_id:
            raise MaintenanceError(
                REASON_COMMIT_MISSING,
                "a Submission Work commit pins a different Playground commit",
            )


# --- public operations ------------------------------------------------------


def create_backup(data_dir, destination, *, service_stopped=False):
    """Create one offline backup of ``data_dir`` at ``destination``.

    ``service_stopped`` must be the literal ``True``; the service must be
    stopped because the database, repository and artifacts are read without a
    global lock.  ``destination`` must not exist yet, must live outside
    ``data_dir`` (and therefore outside every copied root), and is never
    overwritten: the recovery set is built and fsynced in a temporary sibling
    directory and only then atomically renamed into place.  A failure removes
    the temporary directory, so no partially completed backup is ever left
    behind.  This function records a manifest but does not validate the
    finished recovery set; call :func:`validate_backup` for that.
    """

    _require_service_stopped(service_stopped)

    data_dir = Path(data_dir)
    destination = Path(destination)

    if os.path.lexists(destination):
        raise MaintenanceError(
            REASON_DESTINATION_EXISTS,
            "the backup destination already exists and is never overwritten",
        )

    parent = destination.parent
    if parent.is_symlink() or not parent.is_dir():
        raise MaintenanceError(
            REASON_DESTINATION_INVALID,
            "the backup destination parent must be an existing directory",
        )

    if data_dir.is_symlink() or not data_dir.is_dir():
        raise MaintenanceError(
            REASON_DATA_DIR_INVALID, "the data directory does not exist"
        )

    database = data_dir / DATABASE_FILENAME
    git_directory = data_dir / REPOSITORY_DIRNAME
    artifacts = data_dir / ARTIFACTS_DIRNAME

    resolved_data_root = os.path.realpath(data_dir)
    # The destination must stay outside the tree being copied.  Otherwise the
    # temporary build directory would be created inside a copied root and the
    # recursive copy would descend into itself until the path length limit is
    # hit, writing unbounded duplicate data.  Resolve before creating anything.
    if _contained(resolved_data_root, os.path.realpath(destination)):
        raise MaintenanceError(
            REASON_DESTINATION_INVALID,
            "the backup destination must be outside the data directory",
        )
    if not _contained(resolved_data_root, database):
        raise MaintenanceError(
            REASON_PATH_ESCAPE, "the database escapes the data directory"
        )
    if git_directory.is_symlink() or not git_directory.is_dir():
        raise MaintenanceError(
            REASON_DATA_DIR_INVALID, "the Git repository does not exist"
        )
    if not _contained(resolved_data_root, git_directory):
        raise MaintenanceError(
            REASON_PATH_ESCAPE, "the repository escapes the data directory"
        )
    if artifacts.is_symlink():
        raise MaintenanceError(
            REASON_PATH_ESCAPE, "the artifacts directory is a symbolic link"
        )
    if os.path.lexists(artifacts) and not artifacts.is_dir():
        raise MaintenanceError(
            REASON_DATA_DIR_INVALID,
            "the artifacts path is not a directory",
        )
    if artifacts.is_dir() and not _contained(
        resolved_data_root, artifacts
    ):
        raise MaintenanceError(
            REASON_PATH_ESCAPE, "the artifacts directory escapes the data "
            "directory",
        )

    prefix = f".{destination.name}."
    temporary = Path(
        tempfile.mkdtemp(dir=parent, prefix=prefix, suffix=".tmp")
    )
    parent_root = os.path.realpath(parent)
    reserved = False
    try:
        _copy_database(database, temporary / DATABASE_FILENAME)
        _copy_repository(
            git_directory, temporary / REPOSITORY_DIRNAME
        )
        if artifacts.is_dir():
            _copy_tree(artifacts, temporary / ARTIFACTS_DIRNAME)
        entries = _build_manifest(temporary)
        _write_manifest(temporary, entries)
        _fsync_tree(temporary)

        # mkdir reserves the destination atomically; a concurrent or existing
        # backup therefore makes this fail instead of being replaced.
        try:
            os.mkdir(destination)
        except FileExistsError:
            raise MaintenanceError(
                REASON_DESTINATION_EXISTS,
                "the backup destination already exists and is never "
                "overwritten",
            ) from None
        except OSError as error:
            raise MaintenanceError(
                REASON_DESTINATION_INVALID,
                "could not reserve the backup destination",
            ) from error
        reserved = True
        os.rename(temporary, destination)
        reserved = False
        _fsync_directory(parent)
    except BaseException:
        _discard_temporary(temporary, parent_root, prefix)
        if reserved:
            try:
                os.rmdir(destination)
            except OSError:
                pass
        raise

    return destination


def validate_backup(path):
    """Validate one completed backup and return its manifest mapping.

    Validation checks every manifest hash and size, rejects missing, changed,
    or extra files and symbolic links, opens the SQLite copy read-only, runs a
    Git integrity check, and verifies that every commit id referenced by the
    database resolves inside the backed-up repository.  Any problem raises
    :class:`MaintenanceError` with a stable reason code.
    """

    backup = Path(path)
    if backup.is_symlink() or not backup.is_dir():
        raise MaintenanceError(
            REASON_MANIFEST_INVALID, "the backup path is not a directory"
        )

    manifest_path = backup / MANIFEST_FILENAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise MaintenanceError(
            REASON_MANIFEST_INVALID, "the backup manifest is missing"
        )
    try:
        mapping = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise MaintenanceError(
            REASON_MANIFEST_INVALID, "the backup manifest is not valid JSON"
        ) from None

    entries = _parse_manifest(mapping)
    _verify_files(backup, entries)

    artifacts = backup / ARTIFACTS_DIRNAME
    if os.path.lexists(artifacts) and (
        artifacts.is_symlink() or not artifacts.is_dir()
    ):
        raise MaintenanceError(
            REASON_DATA_DIR_INVALID,
            "the artifacts path is not a directory",
        )

    connection = _open_read_only(backup / DATABASE_FILENAME)
    try:
        _validate_database(connection)
        repository = _validate_repository(backup / REPOSITORY_DIRNAME)
        _validate_commit_references(connection, repository)
    finally:
        connection.close()

    return mapping


def restore_backup(backup_path, data_dir, *, service_stopped=False):
    """Restore a validated backup into a new, empty data directory.

    The target must not exist, or must be an existing empty directory.  A
    non-empty target is refused so a live data directory is never deleted or
    replaced.  The recovery set is rebuilt and validated in a fresh temporary
    sibling directory and only then atomically renamed into place, so a
    failure at any point leaves the target absent or still empty and no
    partially populated directory behind.
    """

    _require_service_stopped(service_stopped)

    backup = Path(backup_path)
    data_dir = Path(data_dir)

    # Symmetric guard to :func:`create_backup`: a restore target inside the
    # backup would make the copy write into the very tree it reads from.
    backup_root = os.path.realpath(backup)
    data_root = os.path.realpath(data_dir)
    if _contained(backup_root, data_root):
        raise MaintenanceError(
            REASON_RESTORE_TARGET_INVALID,
            "the restore target must be outside the backup",
        )

    mapping = validate_backup(backup)
    entries = _parse_manifest(mapping)

    if data_dir.is_symlink():
        raise MaintenanceError(
            REASON_RESTORE_TARGET_INVALID,
            "the restore target must not be a symbolic link",
        )
    target_exists = os.path.lexists(data_dir)
    if target_exists:
        if not data_dir.is_dir():
            raise MaintenanceError(
                REASON_RESTORE_TARGET_INVALID,
                "the restore target must be an empty directory",
            )
        if any(data_dir.iterdir()):
            raise MaintenanceError(
                REASON_RESTORE_TARGET_INVALID,
                "refusing to restore over a non-empty data directory",
            )

    parent = data_dir.parent
    if parent.is_symlink() or not parent.is_dir():
        parent.mkdir(parents=True, exist_ok=True)
    parent_root = os.path.realpath(parent)

    prefix = f".{data_dir.name}."
    temporary = Path(
        tempfile.mkdtemp(dir=parent, prefix=prefix, suffix=".tmp")
    )
    try:
        # The manifest is copied so the rebuilt set can be validated as a
        # complete backup before it is published.
        _copy_file(
            backup / MANIFEST_FILENAME,
            temporary / MANIFEST_FILENAME,
        )
        temporary_root = os.path.realpath(temporary)
        for entry in entries:
            source = backup / entry["path"]
            target = temporary / entry["path"]
            if source.is_symlink() or not source.is_file():
                raise MaintenanceError(
                    REASON_FILE_MISSING, "a manifest file is missing"
                )
            if not _contained(backup_root, source) or not _contained(
                temporary_root, target
            ):
                raise MaintenanceError(
                    REASON_PATH_ESCAPE, "a restore path escapes its root"
                )
            _copy_verified_file(source, target, entry)

        # A compacted bare repository may store every ref in ``packed-refs``.
        # Empty directories are intentionally absent from the file manifest,
        # but Git still requires these two structural roots to recognize the
        # restored directory as a repository.
        restored_repository = temporary / REPOSITORY_DIRNAME
        (restored_repository / "objects").mkdir(exist_ok=True)
        (restored_repository / "refs").mkdir(exist_ok=True)
        _fsync_tree(temporary)

        # Validate the completed recovery set before it becomes visible.
        validate_backup(temporary)

        # Final commit point: rename over a missing or still-empty target.
        os.rename(temporary, data_dir)
        _fsync_directory(parent)
    except BaseException:
        _discard_temporary(temporary, parent_root, prefix)
        raise
    return data_dir


def _copy_verified_file(source, target, entry):
    target.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    total = 0
    with open(source, "rb") as reader, open(target, "wb") as writer:
        for chunk in iter(lambda: reader.read(_CHUNK_BYTES), b""):
            total += len(chunk)
            hasher.update(chunk)
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())

    if total != entry["size"] or hasher.hexdigest() != entry["sha256"]:
        try:
            target.unlink()
        except OSError:
            pass
        raise MaintenanceError(
            REASON_FILE_CHANGED, "a file changed while it was restored"
        )
    os.chmod(target, os.stat(source).st_mode & 0o777)


def _discard_temporary(path, parent_root, prefix):
    """Remove our own temporary directory, never anything else."""

    resolved = os.path.realpath(path)
    if os.path.dirname(resolved) != parent_root:
        return
    if not os.path.basename(resolved).startswith(prefix):
        return
    shutil.rmtree(resolved, ignore_errors=True)


# --- Offline health and repository compaction ------------------------------


def _live_database_connection(data_dir):
    database = data_dir / DATABASE_FILENAME
    if database.is_symlink() or not database.is_file():
        raise MaintenanceError(REASON_DATABASE_INVALID, "database is missing")
    return _open_read_only(database)


def _resolve_ref(repository, ref):
    try:
        completed = repository_module._run_git(
            ("rev-parse", "--verify", "--quiet", ref), cwd=repository.path
        )
    except repository_module.RepositoryError as error:
        raise MaintenanceError(
            REASON_GIT_INVALID, "could not resolve a reference"
        ) from error
    if completed.returncode != 0:
        raise MaintenanceError(REASON_GIT_INVALID, "a reference does not resolve")
    commit_id = completed.stdout.strip().decode("ascii", "replace")
    try:
        resolved = repository_module.resolve_commit(repository, commit_id)
    except repository_module.RepositoryError as error:
        raise MaintenanceError(
            REASON_GIT_INVALID, "a reference does not name a commit"
        ) from error
    if resolved != commit_id:
        raise MaintenanceError(
            REASON_GIT_INVALID, "a reference does not name a commit"
        )
    return commit_id


def _linear_history(repository, ref, *, stop=None):
    """Return one bounded, non-merge first-parent history."""

    try:
        completed = repository_module._run_git(
            (
                "rev-list",
                "--first-parent",
                "--parents",
                f"--max-count={_MAX_MAINTENANCE_COMMITS + 1}",
                ref,
            ),
            cwd=repository.path,
        )
    except repository_module.RepositoryError as error:
        raise MaintenanceError(
            REASON_GIT_INVALID, "could not read reference history"
        ) from error
    if completed.returncode != 0:
        raise MaintenanceError(REASON_GIT_INVALID, "could not read reference history")
    lines = completed.stdout.decode("ascii", "replace").splitlines()
    if len(lines) > _MAX_MAINTENANCE_COMMITS:
        raise MaintenanceError(REASON_GIT_INVALID, "reference history is too long")

    commits = []
    reached_stop = False
    for line in lines:
        fields = line.split()
        if not fields:
            raise MaintenanceError(REASON_GIT_INVALID, "malformed commit history")
        commit_id, parents = fields[0], fields[1:]
        if commit_id == stop:
            reached_stop = True
            break
        if len(parents) > 1 or (stop is not None and len(parents) != 1):
            raise MaintenanceError(REASON_GIT_INVALID, "commit history is not linear")
        _require_commit(repository, commit_id)
        commits.append(commit_id)
        if not parents:
            break
    if stop is not None and not reached_stop:
        raise MaintenanceError(
            REASON_COMMIT_MISSING, "Work history does not reach its Playground"
        )
    return commits


def _validate_playground_commit(repository, commit_id):
    try:
        tree = works._read_playground_tree(repository, commit_id)
        bundles.validate_bundle(
            bundles.BundleEntry(path, data) for path, data in tree.items()
        )
    except (
        bundles.BundleValidationError,
        works.WorkError,
        repository_module.RepositoryError,
    ) as error:
        raise MaintenanceError(
            REASON_GIT_INVALID, "Playground commit is invalid"
        ) from error


def _validate_work_commit(repository, commit_id, playground_commit_id):
    try:
        if _work_metadata_pin(repository, commit_id) != playground_commit_id:
            raise MaintenanceError(
                REASON_COMMIT_MISSING,
                "Work history changed its pinned Playground commit",
            )
        tree = works._read_work_tree(repository, commit_id)
        playground_tree = works._read_playground_tree(
            repository, playground_commit_id
        )
        if tree.get(works.WORK_MANIFEST_PATH) != works._work_manifest(
            playground_commit_id
        ):
            raise MaintenanceError(REASON_GIT_INVALID, "Work metadata is invalid")
        if any(tree.get(path) != data for path, data in playground_tree.items()):
            raise MaintenanceError(
                REASON_GIT_INVALID, "Work does not contain its pinned Playground"
            )
        work_entries = [
            works.WorkEntry(path, data)
            for path, data in tree.items()
            if path.startswith(works.WORK_PREFIX)
        ]
        normalized_work = works.validate_work_files(work_entries)
        expected_paths = set(playground_tree) | {works.WORK_MANIFEST_PATH} | set(
            normalized_work
        )
        if set(tree) != expected_paths:
            raise MaintenanceError(REASON_GIT_INVALID, "Work tree is invalid")
    except MaintenanceError:
        raise
    except (works.WorkError, repository_module.RepositoryError) as error:
        raise MaintenanceError(REASON_GIT_INVALID, "Work commit is invalid") from error


def _validate_ref_namespace(repository):
    """Validate every documented ref and reject every other namespace."""

    prefix_playgrounds = "refs/heads/playgrounds/"
    prefix_works = "refs/heads/works/"
    validated_playgrounds = set()
    validated_works = set()
    try:
        refs = repository_module.list_refs(repository)
    except repository_module.RepositoryError as error:
        raise MaintenanceError(REASON_GIT_INVALID, "could not list references") from error

    for ref in refs:
        head = _resolve_ref(repository, ref)
        if ref.startswith(prefix_playgrounds):
            playground_id = ref[len(prefix_playgrounds):]
            try:
                expected_ref = repository.playground_ref(playground_id)
            except repository_module.RepositoryError as error:
                raise MaintenanceError(
                    REASON_GIT_INVALID, "unexpected Playground reference"
                ) from error
            if expected_ref != ref:
                raise MaintenanceError(
                    REASON_GIT_INVALID, "unexpected Playground reference"
                )
            for commit_id in _linear_history(repository, ref):
                if commit_id not in validated_playgrounds:
                    _validate_playground_commit(repository, commit_id)
                    validated_playgrounds.add(commit_id)
        elif ref.startswith(prefix_works):
            remainder = ref[len(prefix_works):]
            playground_id, separator, student_id = remainder.partition("/")
            try:
                expected_ref = repository.work_ref(playground_id, student_id)
                playground_ref = repository.playground_ref(playground_id)
            except repository_module.RepositoryError as error:
                raise MaintenanceError(
                    REASON_GIT_INVALID, "unexpected Work reference"
                ) from error
            if not separator or expected_ref != ref:
                raise MaintenanceError(REASON_GIT_INVALID, "unexpected Work reference")
            playground_commit_id = _work_metadata_pin(repository, head)
            _require_ancestor(repository, playground_commit_id, playground_ref)
            if playground_commit_id not in validated_playgrounds:
                _validate_playground_commit(repository, playground_commit_id)
                validated_playgrounds.add(playground_commit_id)
            for commit_id in _linear_history(
                repository, ref, stop=playground_commit_id
            ):
                if commit_id not in validated_works:
                    _validate_work_commit(
                        repository, commit_id, playground_commit_id
                    )
                    validated_works.add(commit_id)
        else:
            raise MaintenanceError(
                REASON_GIT_INVALID, "unexpected reference namespace"
            )


def validate_data_dir(data_dir, *, service_stopped=False):
    """Validate one live data directory without creating a backup.

    Returns a small counts summary.  ``generated/`` is intentionally ignored:
    it is disposable and must neither make validation pass nor fail.
    """

    _require_service_stopped(service_stopped)

    data_dir = Path(data_dir)
    if data_dir.is_symlink() or not data_dir.is_dir():
        raise MaintenanceError(
            REASON_DATA_DIR_INVALID, "the data directory does not exist"
        )
    database = data_dir / DATABASE_FILENAME
    git_directory = data_dir / REPOSITORY_DIRNAME
    artifacts_directory = data_dir / ARTIFACTS_DIRNAME
    data_root = os.path.realpath(data_dir)

    for managed_path in (database, git_directory, artifacts_directory):
        if os.path.lexists(managed_path) and not _contained(
            data_root, managed_path
        ):
            raise MaintenanceError(
                REASON_PATH_ESCAPE, "a managed path escapes the data directory"
            )

    try:
        permissions.ensure_private_directory(data_dir)
        if git_directory.exists():
            permissions.ensure_private_directory(git_directory)
        if artifacts_directory.exists():
            permissions.ensure_private_directory(artifacts_directory)
        if database.exists():
            permissions.ensure_private_file(database)
    except permissions.PermissionPolicyError as error:
        raise MaintenanceError(
            REASON_DATA_DIR_INVALID, "managed permissions are invalid"
        ) from error

    connection = _live_database_connection(data_dir)
    try:
        _validate_database(connection)
        repository = _validate_repository(git_directory)
        _validate_commit_references(connection, repository)
        releases = connection.execute(
            "SELECT COUNT(*) FROM releases"
        ).fetchone()[0]
        submissions = connection.execute(
            "SELECT COUNT(*) FROM submissions"
        ).fetchone()[0]
    finally:
        connection.close()

    _validate_ref_namespace(repository)

    try:
        entries = artifacts.index_artifacts(artifacts_directory)
        for entry in entries:
            artifacts.verify_artifact(artifacts_directory, entry)
    except artifacts.ArtifactError as error:
        raise MaintenanceError(
            REASON_DATA_DIR_INVALID, "artifact storage is invalid"
        ) from error

    return {
        "releases": releases,
        "submissions": submissions,
        "artifacts": len(entries),
    }


def _capture_refs(repository):
    refs = {}
    try:
        names = repository_module.list_refs(repository)
    except repository_module.RepositoryError as error:
        raise MaintenanceError(REASON_GIT_INVALID, "could not list references") from error
    for ref in names:
        refs[ref] = _resolve_ref(repository, ref)
    return refs


def _database_commit_ids(data_dir):
    connection = _live_database_connection(data_dir)
    try:
        commits = set()
        for row in connection.execute(
            "SELECT DISTINCT playground_commit_id FROM releases"
        ).fetchall():
            commits.add(row[0])
        for row in connection.execute(
            "SELECT DISTINCT work_commit_id FROM submissions"
        ).fetchall():
            commits.add(row[0])
    finally:
        connection.close()
    return commits


def compact_repository(data_dir, *, service_stopped=False):
    """Validate, run a safe ``git gc``, and prove nothing authoritative moved."""

    _require_service_stopped(service_stopped)

    data_dir = Path(data_dir)
    summary = validate_data_dir(data_dir, service_stopped=True)
    git_directory = data_dir / REPOSITORY_DIRNAME
    repository = _validate_repository(git_directory)
    before = _capture_refs(repository)
    commits = _database_commit_ids(data_dir)

    try:
        repository_module.compact(repository)
    except repository_module.RepositoryError as error:
        raise MaintenanceError(
            REASON_GIT_INVALID, "repository compaction failed"
        ) from error

    after_summary = validate_data_dir(data_dir, service_stopped=True)
    if after_summary != summary:
        raise MaintenanceError(
            REASON_DATA_DIR_INVALID, "authority counts changed during compaction"
        )
    repository = _validate_repository(git_directory)
    after = _capture_refs(repository)
    if before != after:
        raise MaintenanceError(
            REASON_GIT_INVALID, "references changed during compaction"
        )
    for commit_id in commits:
        try:
            resolved = repository_module.resolve_commit(repository, commit_id)
        except repository_module.RepositoryError as error:
            raise MaintenanceError(
                REASON_COMMIT_MISSING,
                "a commit became unreadable during compaction",
            ) from error
        if resolved is None:
            raise MaintenanceError(
                REASON_COMMIT_MISSING,
                "a commit disappeared during compaction",
            )
    return summary


def _run_cli(argv=None):
    parser = argparse.ArgumentParser(prog="maintenance.py")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "compact"):
        subparser = subcommands.add_parser(name)
        subparser.add_argument("--data-dir", required=True)
        subparser.add_argument("--service-stopped", action="store_true")
    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "validate":
            summary = validate_data_dir(
                arguments.data_dir,
                service_stopped=arguments.service_stopped,
            )
        else:
            summary = compact_repository(
                arguments.data_dir,
                service_stopped=arguments.service_stopped,
            )
    except MaintenanceError as error:
        print(json.dumps({"error": error.reason}, sort_keys=True))
        return 1
    print(json.dumps({"status": "ok", "summary": summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())

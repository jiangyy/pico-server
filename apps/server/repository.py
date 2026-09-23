"""Bootstrap and validate the private bare Git repository.

Only the Python standard library and the installed ``git`` executable are used.
This module deliberately exposes no generic ref helper: callers may only build
a Playground reference through the validated helper on :class:`Repository`.
"""

import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from bundles import BundleEntry, BundleValidationError, validate_bundle
import permissions


DEFAULT_REPOSITORY_PATH = Path("data/picopg.git")
GIT_TIMEOUT_SECONDS = 30

# Git is authoritative for ref syntax, but reject obviously unsafe identifiers
# before starting a subprocess. The set matches the characters Git forbids in a
# ref component.
_REF_METACHARACTERS = frozenset(" ~^:?*[\\")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")

# Git object IDs are lowercase hexadecimal.  Requiring the exact full-width
# form before a subprocess stops a symbolic name such as ``HEAD``, a short
# prefix, or an option from being resolved as if it were an object id.
_OBJECT_ID_PATTERN = re.compile(r"\A[0-9a-f]{40}\Z|\A[0-9a-f]{64}\Z")

#: implementation.md §2: the stable, per-Playground reachability root. It converges to
#: the database's `current_version_id`; it never leads it.
HISTORY_REF_PREFIX = "refs/picopg/history/"
#: implementation.md §2: the temporary root for a Version that is prepared but not yet
#: accepted. One per publish attempt, removed once the attempt is settled.
PENDING_REF_PREFIX = "refs/picopg/pending/"

#: An Assignment's history. Its own namespace on purpose: assignment ids and
#: playground ids are independent, so one shared prefix would let either kind's
#: history overwrite the other's.
ASSIGNMENT_REF_PREFIX = "refs/picopg/assignment/"


def _require_publish_id(publish_id):
    """A publish id names a pending ref, so it must be a safe single segment.

    It identifies one publish *attempt* — it is not a domain event and nothing
    outside this file's two ref operations reads it.
    """

    if type(publish_id) is not str or not publish_id:
        raise RepositoryError("publish id must be a non-empty string")
    if "/" in publish_id or "\\" in publish_id or publish_id.startswith("."):
        raise RepositoryError("publish id must be a single path-safe segment")
    return publish_id


class RepositoryError(Exception):
    """Raised when the private repository cannot be opened or validated."""


class Repository:
    """Handle for one private bare Git repository."""

    def __init__(self, path):
        self.path = Path(path)

    def history_ref(self, opaque_id):
        """Return the stable history ref for a Playground.

        Everything a Playground has accepted stays reachable from here. It is
        *not* the addressing scheme for a Version — a Version is read by its own
        id (implementation.md §2) — it is the reachability root that keeps the Git GC
        away and the thing `commit_in_playground_history` consults.
        """
        validate_playground_id(opaque_id)
        ref = f"{HISTORY_REF_PREFIX}{opaque_id}"
        _check_ref_format(ref)
        return ref

    def pending_ref(self, publish_id):
        """Return the temporary ref that protects one prepared Version.

        A publish creates its commit before the database decides to accept it,
        and in that window nothing else references the commit. This ref is what
        keeps it alive; recovery removes it, so it is never a second history.
        """
        _require_publish_id(publish_id)
        ref = f"{PENDING_REF_PREFIX}{publish_id}"
        _check_ref_format(ref)
        return ref

    def assignment_ref(self, opaque_id):
        """Return the stable history ref for an Assignment.

        The same job as `history_ref`, in its own namespace. A Version is still
        read by its own id; this ref is only the reachability root.
        """
        validate_playground_id(opaque_id)
        ref = f"{ASSIGNMENT_REF_PREFIX}{opaque_id}"
        _check_ref_format(ref)
        return ref

    def legacy_ref(self, opaque_id):
        """The ref this project wrote before it had a namespaced layout.

        Read once during migration. Nothing writes it any more.
        """
        validate_playground_id(opaque_id)
        ref = f"refs/heads/playgrounds/{opaque_id}"
        _check_ref_format(ref)
        return ref

def open_repository(path=DEFAULT_REPOSITORY_PATH):
    """Open or idempotently initialize the private bare repository at ``path``."""
    path = Path(path)

    if path.exists():
        if not path.is_dir():
            raise RepositoryError("repository path is not a directory")
        if not _is_bare_repository(path):
            raise RepositoryError("path is not a bare Git repository")
        try:
            permissions.ensure_private_directory(path)
        except permissions.PermissionPolicyError as error:
            raise RepositoryError("repository permissions are invalid") from error
        return Repository(path)

    _initialize_bare_repository(path)
    if not _is_bare_repository(path):
        raise RepositoryError("repository was not initialized as bare")
    try:
        permissions.ensure_private_directory(path)
    except permissions.PermissionPolicyError as error:
        raise RepositoryError("repository permissions are invalid") from error
    return Repository(path)


def _initialize_bare_repository(path):
    completed = _run_git(("init", "--bare", "--", str(path)))
    if completed.returncode != 0:
        raise RepositoryError("could not initialize bare repository")


def _is_bare_repository(path):
    completed = _run_git(("rev-parse", "--is-bare-repository"), cwd=path)
    if completed.returncode != 0:
        return False
    return completed.stdout.strip() == b"true"


def _validate_identifier(value, label):
    if not isinstance(value, str) or not value:
        raise RepositoryError(f"{label} must be a non-empty string")
    if value in (".", ".."):
        raise RepositoryError(f"{label} must not be a relative path")
    if "/" in value or "\\" in value:
        raise RepositoryError(f"{label} must not contain a slash")
    if any(character in value for character in _REF_METACHARACTERS):
        raise RepositoryError(f"{label} contains a ref metacharacter")
    if ".." in value or "@{" in value:
        raise RepositoryError(f"{label} contains a forbidden sequence")
    if value.startswith(".") or value.endswith("."):
        raise RepositoryError(f"{label} must not begin or end with a dot")
    if value.endswith(".lock"):
        raise RepositoryError(f"{label} must not end with .lock")
    if _CONTROL_CHARACTERS.search(value):
        raise RepositoryError(f"{label} contains a control character")


# Playground identifiers are the only opaque IDs that must simultaneously be a
# Git ref component, an HTTP path segment, and part of an idempotency endpoint.
# The exact rule is deliberately narrower than ``_validate_identifier`` so a
# metadata row can never name a route or ref the Server cannot represent.
_PLAYGROUND_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def valid_playground_id(value):
    """Return ``True`` for an exact, representable Playground identifier."""

    if type(value) is not str:
        return False
    if _PLAYGROUND_ID_PATTERN.match(value) is None:
        return False
    if value.endswith("."):
        return False
    if ".." in value:
        return False
    if value.casefold().endswith(".lock"):
        return False
    return True


def validate_playground_id(value):
    """Return ``value`` when it is a valid Playground identifier."""

    if not valid_playground_id(value):
        raise RepositoryError("playground id is not valid")
    return value


def _check_ref_format(ref):
    completed = _run_git(("check-ref-format", ref))
    if completed.returncode != 0:
        raise RepositoryError("reference is not accepted by Git")


def _run_git(arguments, cwd=None, stdin=None, environment=None):
    effective_environment = _sanitized_git_environment()
    if environment is not None:
        if set(environment) - _COMMIT_ENVIRONMENT_KEYS:
            raise RepositoryError("git environment override is not allowed")
        effective_environment.update(environment)
    options = {
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "check": False,
        "shell": False,
        "timeout": GIT_TIMEOUT_SECONDS,
        "env": effective_environment,
    }
    if stdin is None:
        options["stdin"] = subprocess.DEVNULL
    else:
        options["input"] = stdin
    try:
        return subprocess.run(["git", *arguments], **options)
    except (OSError, subprocess.SubprocessError) as error:
        raise RepositoryError("git command could not be executed") from error


# --- Playground publishing -------------------------------------------------
#
# Publishing is deliberately a module-level operation rather than a method on
# :class:`Repository`: the class keeps the small, stable public surface that
# callers already rely on, and the low-level plumbing stays private here.

# The only caller-supplied Git variables accepted by :func:`_run_git`.  The
# author/committer identity is constructed inside this module from validated
# metadata; any other override is rejected rather than merged.
_COMMIT_ENVIRONMENT_KEYS = frozenset(
    {
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_DATE",
    }
)

_OBJECT_FORMAT_WIDTHS = {"sha1": 40, "sha256": 64}


def _sanitized_git_environment():
    # Drop every inherited ``GIT_*`` variable instead of blacklisting known
    # redirects: a new Git control variable must not be able to leak in.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_SYSTEM"] = os.devnull
    return environment


@dataclass(frozen=True)
class CommitMetadata:
    """Trusted, deterministic commit identity supplied by the caller.

    Every field must be a plain ``str`` and free of NUL, newline, and other
    control characters so that no value can inject extra commit headers.
    The bundle is never consulted for any of these values.  ``date`` is
    accepted as an alias for ``timestamp``.
    """

    name: str
    email: str
    timestamp: str
    message: str

    def __init__(
        self, name, email, timestamp=None, message=None, *, date=None
    ):
        if timestamp is None:
            timestamp = date
        elif date is not None:
            raise TypeError("pass either timestamp or date, not both")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "email", email)
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "message", message)

    @property
    def date(self):
        return self.timestamp


@dataclass(frozen=True)
class PublishResult:
    """Immutable outcome of publishing one Playground bundle."""

    playground_id: str
    commit_id: str
    no_op: bool


def _validate_commit_metadata(metadata):
    if type(metadata) is not CommitMetadata:
        raise RepositoryError("commit metadata must be CommitMetadata")
    fields = (
        ("name", metadata.name),
        ("email", metadata.email),
        ("timestamp", metadata.timestamp),
        ("message", metadata.message),
    )
    for label, value in fields:
        if type(value) is not str or not value:
            raise RepositoryError(
                f"commit metadata {label} must be a non-empty string"
            )
        if _CONTROL_CHARACTERS.search(value):
            raise RepositoryError(
                f"commit metadata {label} contains a forbidden character"
            )


def _snapshot_bundle(bundle):
    """Revalidate a bundle mapping and return a sorted ``(path, data)`` list.

    The exact read-only mapping type produced by ``validate_bundle`` is
    required, but its contents are not trusted: entries are copied into fresh
    plain values, rebuilt as exact :class:`BundleEntry` objects, and passed
    back through the pure validator.  A caller-crafted mapping proxy cannot
    smuggle in invalid JSON, a different schema/runtime, controls, non-
    normalized paths, or a file/directory collision.  Any failure becomes a
    bounded ``RepositoryError`` before a Git object is written.
    """

    if type(bundle) is not MappingProxyType:
        raise RepositoryError(
            "bundle must be the normalized read-only mapping"
        )

    items = _snapshot_mapping_items(bundle)
    if items is None:
        raise RepositoryError("bundle could not be read")

    entries = []
    for path, data in items:
        if type(path) is not str or type(data) is not bytes:
            raise RepositoryError(
                "bundle entries must be plain str paths and bytes values"
            )
        entries.append(BundleEntry(path, data))

    normalized = _revalidate_bundle(entries)
    if normalized is None:
        raise RepositoryError("bundle failed validation")

    files = list(normalized.items())
    files.sort(key=lambda item: item[0])
    return files


def _snapshot_mapping_items(bundle):
    """Return the mapping's items, or ``None`` with the error dropped."""
    try:
        return list(bundle.items())
    except Exception:
        return None


def _revalidate_bundle(entries):
    """Return the normalized mapping, or ``None`` with the error dropped."""
    try:
        return validate_bundle(entries)
    except BundleValidationError:
        return None


def _bundle_tree(files):
    """Build a nested directory structure, rejecting file/directory clashes."""

    root = {}
    for path, data in files:
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            child = node.get(part)
            if child is None:
                child = {}
                node[part] = child
            elif not isinstance(child, dict):
                raise RepositoryError(
                    "bundle path is both a file and a directory"
                )
            node = child
        leaf = parts[-1]
        if leaf in node:
            raise RepositoryError("bundle path is duplicated")
        node[leaf] = data
    return root


def _write_bundle_blob(repository, data):
    completed = _run_git(
        ("hash-object", "-w", "--stdin"),
        cwd=repository.path,
        stdin=data,
    )
    if completed.returncode != 0:
        raise RepositoryError("could not write bundle blob")
    return completed.stdout.strip().decode("ascii")


def _encode_tree_name(name):
    """Return the UTF-8 bytes of one tree entry name.

    A client value containing a lone surrogate cannot be encoded and would
    otherwise crash the tree writer.  It is surfaced as the module's domain
    error so no caller (publish or Work) ever sees an unhandled
    :class:`UnicodeEncodeError`.
    """

    try:
        return name.encode("utf-8")
    except (UnicodeEncodeError, AttributeError):
        raise RepositoryError("tree entry name cannot be encoded as UTF-8")


def _write_bundle_tree(repository, node):
    # Encode every name before writing any blob so a bad name is rejected
    # before any object write (apart from already written siblings).
    encoded_names = {
        name: _encode_tree_name(name) for name in sorted(node)
    }
    payload = bytearray()
    for name in sorted(node):
        value = node[name]
        if isinstance(value, dict):
            mode = b"040000"
            kind = b"tree"
            object_id = _write_bundle_tree(repository, value)
        else:
            mode = b"100644"
            kind = b"blob"
            object_id = _write_bundle_blob(repository, value)
        payload += b"%s %s %s\t%s\x00" % (
            mode,
            kind,
            object_id.encode("ascii"),
            encoded_names[name],
        )
    completed = _run_git(
        ("mktree", "-z"), cwd=repository.path, stdin=bytes(payload)
    )
    if completed.returncode != 0:
        raise RepositoryError("could not write bundle tree")
    return completed.stdout.strip().decode("ascii")


def _resolve_head(repository, ref):
    completed = _run_git(
        ("rev-parse", "--verify", "--quiet", ref), cwd=repository.path
    )
    if completed.returncode != 0:
        return None
    head = completed.stdout.strip().decode("ascii")
    return head or None


def _commit_tree_id(repository, commit_id):
    completed = _run_git(
        ("rev-parse", f"{commit_id}^{{tree}}"), cwd=repository.path
    )
    if completed.returncode != 0:
        raise RepositoryError("could not resolve playground head tree")
    return completed.stdout.strip().decode("ascii")


def _zero_object_id(repository):
    completed = _run_git(
        ("rev-parse", "--show-object-format"), cwd=repository.path
    )
    if completed.returncode != 0:
        raise RepositoryError("could not determine Git object format")
    object_format = completed.stdout.strip().decode("ascii")
    width = _OBJECT_FORMAT_WIDTHS.get(object_format)
    if width is None:
        raise RepositoryError("unsupported Git object format")
    return "0" * width


def _create_commit(
    repository, tree_id, parent, metadata,
    error_message="could not create playground commit",
):
    arguments = ["commit-tree", tree_id]
    if parent is not None:
        arguments += ["-p", parent]
    environment = {
        "GIT_AUTHOR_NAME": metadata.name,
        "GIT_AUTHOR_EMAIL": metadata.email,
        "GIT_AUTHOR_DATE": metadata.timestamp,
        "GIT_COMMITTER_NAME": metadata.name,
        "GIT_COMMITTER_EMAIL": metadata.email,
        "GIT_COMMITTER_DATE": metadata.timestamp,
    }
    completed = _run_git(
        arguments,
        cwd=repository.path,
        stdin=(metadata.message + "\n").encode("utf-8"),
        environment=environment,
    )
    if completed.returncode != 0:
        raise RepositoryError(error_message)
    return completed.stdout.strip().decode("ascii")


# `PlaygroundHeadChangedError` and the ref compare-and-swap (`_compare_and_swap`,
# `_reference_matches`, `_is_zero_object_id`) used to live here. They reported
# "the history ref moved between the read and the write". The database now
# decides whether a Version is accepted, a publisher whose base is stale is told
# `PlaygroundBaseChangedError`, and the history ref is *converged* onto the
# database's answer rather than compared against it — a ref that is behind is a
# ref to correct, not a race to report.


@dataclass(frozen=True)
class PreparedVersion:
    """A Version built from a bundle, not yet accepted by anything.

    ``publish_id`` is ``None`` exactly when ``no_op`` is true: no commit was
    created, so there is nothing to protect either.
    """

    playground_id: str
    commit_id: str
    publish_id: str | None
    no_op: bool


@dataclass(frozen=True)
class PreparedAssignment:
    """An Assignment Version built and protected, not yet accepted.

    ``publish_id`` is ``None`` exactly when ``no_op`` is true.
    """

    assignment_id: str
    commit_id: str
    publish_id: str | None
    no_op: bool


def prepare_assignment_version(
    repository, assignment_id, markdown, metadata, publish_id, observed_version_id=None
):
    """Prepare an Assignment Version: one ``assignment.md``, one commit, one pending ref.

    An Assignment is not a Playground bundle — there is nothing to validate
    beyond the Markdown itself, which the application层 already parsed — so this
    reuses the tree/commit/ref machinery and nothing else. ``no_op`` is decided
    the same way: the new tree equals the observed Version's tree.
    """

    if not isinstance(repository, Repository):
        raise RepositoryError("repository handle required")
    if type(markdown) is not bytes or len(markdown) == 0:
        raise RepositoryError("assignment markdown must be non-empty bytes")
    _validate_commit_metadata(metadata)
    _require_publish_id(publish_id)

    ref = repository.assignment_ref(assignment_id)

    if observed_version_id is not None:
        head = resolve_commit(repository, observed_version_id)
        if head is None:
            raise RepositoryError("observed version is not a commit")
    else:
        head = _resolve_head(repository, ref)
    head_tree = _commit_tree_id(repository, head) if head is not None else None

    new_tree_id = _write_bundle_tree(repository, {"assignment.md": markdown})
    if head is not None and new_tree_id == head_tree:
        return PreparedAssignment(
            assignment_id=assignment_id, commit_id=head, publish_id=None, no_op=True
        )

    commit_id = _create_commit(repository, new_tree_id, head, metadata)
    create_pending_ref(repository, publish_id, commit_id)
    return PreparedAssignment(
        assignment_id=assignment_id,
        commit_id=commit_id,
        publish_id=publish_id,
        no_op=False,
    )


def commit_in_assignment_history(repository, assignment_id, commit_id):
    """Return whether ``commit_id`` belongs to the named Assignment history."""

    try:
        ref = repository.assignment_ref(assignment_id)
    except RepositoryError:
        return False
    if _resolve_head(repository, ref) is None:
        return False
    completed = _run_git(
        ("merge-base", "--is-ancestor", commit_id, ref), cwd=repository.path
    )
    return completed.returncode == 0


def publish_playground_prepare(
    repository, playground_id, bundle, metadata, publish_id, observed_version_id=None
):
    """Prepare a Version for ``playground_id`` and protect it from the GC.

    Nothing here reads the database and nothing decides that the Version is
    accepted: it builds the commit, hangs a
    ``refs/picopg/pending/<publish_id>`` on it so the garbage collector cannot
    take bytes the database has not ruled on yet, and returns.

    The parent is the Version the *application* says is current —
    ``observed_version_id`` — and only falls back to the history ref for a
    Playground that has no Version yet. The ref is a reachability root, not an
    addressing mechanism, and it lags the database
    between the database's commit and Finalize: a publish that chained onto a
    lagging ref would leave the Version the database had just accepted out of the
    history, unreachable and no longer readable by the clients pinned to it. It
    also makes ``no_op`` answer about the right Version.

    A concurrent publish therefore forks, and the database's compare-and-swap
    decides which side survives; the loser's commit becomes unreachable once its
    pending ref is dropped.

    ``no_op`` means the tree is already the one at the observed Version, so no
    commit and no pending ref are created. ``publish_id`` only has to be what
    the caller will also use to delete the pending ref; recovery never needs it,
    because it finds the Version's Playground by asking the database.
    """

    if not isinstance(repository, Repository):
        raise RepositoryError("repository handle required")
    _validate_commit_metadata(metadata)
    _require_publish_id(publish_id)

    # Re-validates the identifier and returns only the namespaced Playground
    # ref; no caller-controlled ref string reaches Git.
    ref = repository.history_ref(playground_id)

    files = _snapshot_bundle(bundle)
    tree = _bundle_tree(files)

    if observed_version_id is not None:
        head = resolve_commit(repository, observed_version_id)
        if head is None:
            raise RepositoryError("observed version is not a commit")
    else:
        head = _resolve_head(repository, ref)
    head_tree = _commit_tree_id(repository, head) if head is not None else None

    new_tree_id = _write_bundle_tree(repository, tree)

    if head is not None and new_tree_id == head_tree:
        return PreparedVersion(
            playground_id=playground_id, commit_id=head, publish_id=None, no_op=True
        )

    commit_id = _create_commit(repository, new_tree_id, head, metadata)
    create_pending_ref(repository, publish_id, commit_id)
    return PreparedVersion(
        playground_id=playground_id,
        commit_id=commit_id,
        publish_id=publish_id,
        no_op=False,
    )


def publish_playground(repository, playground_id, bundle, metadata):
    """Prepare a Version and converge the history ref onto it.

    A convenience for callers that have already decided to accept the Version
    (tests, and anything that has no database behind it). The service does
    *not* use this: it prepares, lets the database decide, and converges
    afterwards, because converging first is what makes the ref lead the
    database instead of following it.
    """

    publish_id = uuid.uuid4().hex
    prepared = publish_playground_prepare(
        repository, playground_id, bundle, metadata, publish_id
    )
    if not prepared.no_op:
        converge_history_ref(repository, playground_id, prepared.commit_id)
        delete_pending_ref(repository, publish_id)
    return PublishResult(
        playground_id=prepared.playground_id,
        commit_id=prepared.commit_id,
        no_op=prepared.no_op,
    )


# --- The two ref namespaces ------------------------------------------------
#
# Publish is three phases: Git prepares a Version, the database decides whether
# to accept it, Git finalises. The two ref namespaces are what makes that safe:
#
#   refs/picopg/pending/<publish_id> protects a prepared Version until the
#                                    database has answered, then goes away
#   refs/picopg/history/<playground> the stable root, converged *from* the
#                                    database's current_version_id afterwards
#
# Nothing here reads the database, and nothing here decides what is accepted:
# these are reachability operations.


def create_pending_ref(repository, publish_id, commit_id):
    """Point a pending ref at a prepared Version, so the GC cannot take it."""

    if not isinstance(repository, Repository):
        raise RepositoryError("repository handle required")
    _require_object_id(commit_id)
    ref = repository.pending_ref(publish_id)
    completed = _run_git(
        ("update-ref", ref, commit_id, _zero_object_id(repository)),
        cwd=repository.path,
    )
    if completed.returncode != 0:
        # A pending ref is created once, by the attempt that owns this publish
        # id, so a failure here is a repository problem, not a lost race.
        raise RepositoryError("could not create pending reference")


def delete_pending_ref(repository, publish_id):
    """Remove a pending ref, if it is still there."""

    if not isinstance(repository, Repository):
        raise RepositoryError("repository handle required")
    ref = repository.pending_ref(publish_id)
    completed = _run_git(("update-ref", "-d", ref), cwd=repository.path)
    if completed.returncode != 0:
        raise RepositoryError("could not delete pending reference")


def converge_history_ref(repository, playground_id, version_id):
    """Move the history ref to the Version the database says is current.

    Unconditional on purpose: the database is the authority, so the ref follows
    it. There is nothing to compare-and-swap against, because a ref that
    disagrees is a ref that is behind, not a race.
    """

    if not isinstance(repository, Repository):
        raise RepositoryError("repository handle required")
    _require_object_id(version_id)
    ref = repository.history_ref(playground_id)
    completed = _run_git(
        ("update-ref", ref, version_id), cwd=repository.path
    )
    if completed.returncode != 0:
        raise RepositoryError("could not converge history reference")


def converge_assignment_ref(repository, assignment_id, version_id):
    """Move an Assignment's history ref to the Version the database says is current."""

    if not isinstance(repository, Repository):
        raise RepositoryError("repository handle required")
    _require_object_id(version_id)
    ref = repository.assignment_ref(assignment_id)
    completed = _run_git(("update-ref", ref, version_id), cwd=repository.path)
    if completed.returncode != 0:
        raise RepositoryError("could not converge assignment reference")


def list_pending_refs(repository):
    """Return ``[(publish_id, commit_id), ...]`` for every pending ref."""

    if not isinstance(repository, Repository):
        raise RepositoryError("repository handle required")
    completed = _run_git(
        ("for-each-ref", "--format=%(refname) %(objectname)", PENDING_REF_PREFIX),
        cwd=repository.path,
    )
    if completed.returncode != 0:
        raise RepositoryError("could not list pending references")

    pending = []
    for line in completed.stdout.decode("ascii", "replace").splitlines():
        name, _, object_id = line.partition(" ")
        publish_id = name[len(PENDING_REF_PREFIX):]
        if not publish_id or not object_id:
            continue
        pending.append((publish_id, object_id.strip()))
    return pending


def migrate_legacy_refs(repository, playground_ids):
    """Copy the pre-namespace refs into ``refs/picopg/history/`` and drop them.

    Before this layout a Playground's history lived at
    ``refs/heads/playgrounds/<id>``. The history ref is created first and the
    legacy one deleted second, so an interrupted migration leaves the bytes
    reachable either way and running it again finishes the job.

    Returns how many Playgrounds were migrated.
    """

    if not isinstance(repository, Repository):
        raise RepositoryError("repository handle required")

    migrated = 0
    for playground_id in playground_ids:
        try:
            legacy = repository.legacy_ref(playground_id)
            history = repository.history_ref(playground_id)
        except RepositoryError:
            continue
        version_id = _resolve_head(repository, legacy)
        if version_id is None:
            continue
        # Only when the history ref is missing: a Playground whose history ref
        # already exists is authoritative, and the legacy ref is stale.
        if _resolve_head(repository, history) is None:
            converge_history_ref(repository, playground_id, version_id)
        completed = _run_git(("update-ref", "-d", legacy), cwd=repository.path)
        if completed.returncode != 0:
            raise RepositoryError("could not delete legacy reference")
        migrated += 1
    return migrated


# --- Read-only access to published commits ---------------------------------
#
# A commit id alone never reaches Git content: the caller checks that the
# commit belongs to a Playground's published history first, then these helpers
# list that commit's tree and read its blobs.  Nothing here writes.


@dataclass(frozen=True)
class TreeEntry:
    """One recursively listed entry of a commit's tree.

    ``size`` is the blob size reported by Git and is ``None`` for entries that
    are not blobs (for example a submodule gitlink).
    """

    path: str
    mode: str
    kind: str
    object_id: str
    size: int | None


def commit_in_playground_history(repository, playground_id, commit_id):
    """Return whether ``commit_id`` belongs to the named Playground history.

    A published Playground is the only visibility rule left: its ref history
    is the whole set of commits any caller may read, so an unpublished or
    unknown commit and a missing Playground get the same answer.
    """

    try:
        ref = repository.history_ref(playground_id)
    except RepositoryError:
        return False
    if _resolve_head(repository, ref) is None:
        return False
    completed = _run_git(
        ("merge-base", "--is-ancestor", commit_id, ref), cwd=repository.path
    )
    return completed.returncode == 0


def resolve_commit(repository, commit_id):
    """Return the exact commit id, or ``None`` when it does not exist.

    ``commit_id`` must already be a full lowercase object id.  Symbolic names
    and short prefixes are rejected instead of being resolved.
    """
    if not isinstance(repository, Repository):
        raise RepositoryError("repository handle required")
    commit_id = _require_object_id(commit_id)
    completed = _run_git(
        ("rev-parse", "--verify", "--quiet", f"{commit_id}^{{commit}}"),
        cwd=repository.path,
    )
    if completed.returncode != 0:
        return None
    resolved = completed.stdout.strip().decode("ascii", "replace")
    if resolved != commit_id:
        return None
    return resolved


def list_commit_tree(repository, commit_id):
    """Recursively list one exact commit's tree without any checkout.

    Returns :class:`TreeEntry` values in Git's own tree order.  Callers that
    need path order sort the result themselves.  A missing commit or a tree-ish
    that is not a commit raises :class:`RepositoryError`.
    """
    if not isinstance(repository, Repository):
        raise RepositoryError("repository handle required")
    commit_id = _require_object_id(commit_id)
    if resolve_commit(repository, commit_id) is None:
        raise RepositoryError("commit does not exist")
    completed = _run_git(
        ("ls-tree", "-r", "-l", "-z", commit_id), cwd=repository.path
    )
    if completed.returncode != 0:
        raise RepositoryError("could not list commit tree")
    return _parse_tree_listing(completed.stdout)


def read_blobs(repository, object_ids):
    """Read blob contents by exact object id in one bounded batch.

    Returns a mapping from the requested object id to its exact bytes.
    Duplicate ids are read once.  A non-blob or missing object raises
    :class:`RepositoryError` instead of returning partial content.
    """
    if not isinstance(repository, Repository):
        raise RepositoryError("repository handle required")
    requested = []
    seen = set()
    for object_id in object_ids:
        object_id = _require_object_id(object_id)
        if object_id not in seen:
            seen.add(object_id)
            requested.append(object_id)
    if not requested:
        return {}
    payload = ("\n".join(requested) + "\n").encode("ascii")
    completed = _run_git(
        ("cat-file", "--batch"), cwd=repository.path, stdin=payload
    )
    if completed.returncode != 0:
        raise RepositoryError("could not read commit blobs")
    return _parse_batch_output(completed.stdout, requested)


def _require_object_id(value):
    if type(value) is not str or _OBJECT_ID_PATTERN.match(value) is None:
        raise RepositoryError("object id must be a full Git object id")
    return value


def _parse_tree_listing(payload):
    if type(payload) is not bytes:
        raise RepositoryError("tree listing must be bytes")
    entries = []
    for record in payload.split(b"\x00"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise RepositoryError("malformed tree entry")
        fields = metadata.split()
        if len(fields) != 4:
            raise RepositoryError("malformed tree entry")
        try:
            mode = fields[0].decode("ascii")
            kind = fields[1].decode("ascii")
            object_id = fields[2].decode("ascii")
            size_field = fields[3].decode("ascii")
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RepositoryError("malformed tree entry") from error
        if _OBJECT_ID_PATTERN.match(object_id) is None:
            raise RepositoryError("malformed tree entry")
        if size_field == "-":
            size = None
        else:
            try:
                size = int(size_field)
            except ValueError as error:
                raise RepositoryError("malformed tree entry") from error
            if size < 0:
                raise RepositoryError("malformed tree entry")
        entries.append(
            TreeEntry(
                path=path,
                mode=mode,
                kind=kind,
                object_id=object_id,
                size=size,
            )
        )
    return entries


def _parse_batch_output(payload, requested):
    blobs = {}
    position = 0
    for object_id in requested:
        newline = payload.find(b"\n", position)
        if newline < 0:
            raise RepositoryError("malformed batch response")
        header = payload[position:newline].split()
        position = newline + 1
        if len(header) < 3:
            raise RepositoryError("blob is missing from repository")
        try:
            reported_id = header[0].decode("ascii")
            kind = header[1].decode("ascii")
            size = int(header[2])
        except (UnicodeDecodeError, ValueError) as error:
            raise RepositoryError("malformed batch response") from error
        if reported_id != object_id or kind != "blob" or size < 0:
            raise RepositoryError("malformed batch response")
        end = position + size
        if end > len(payload) or payload[end:end + 1] != b"\n":
            raise RepositoryError("malformed batch response")
        blobs[object_id] = bytes(payload[position:end])
        position = end + 1
    return blobs


# --- Offline maintenance ---------------------------------------------------

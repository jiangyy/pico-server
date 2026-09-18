"""Bootstrap and validate the private bare Git repository.

Only the Python standard library and the installed ``git`` executable are used.
This module deliberately exposes no generic ref helper: callers may only build
a Playground reference through the validated helper on :class:`Repository`.
"""

import os
import re
import subprocess
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


class RepositoryError(Exception):
    """Raised when the private repository cannot be opened or validated."""


class Repository:
    """Handle for one private bare Git repository."""

    def __init__(self, path):
        self.path = Path(path)

    def playground_ref(self, opaque_id):
        """Return the Playground ref for ``opaque_id``."""
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


class PlaygroundHeadChangedError(RepositoryError):
    """The Playground ref moved between reading HEAD and the CAS update."""

    reason = "playground_head_changed"

    def __init__(self, message="playground head changed"):
        super().__init__(message)


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


def _is_zero_object_id(object_id):
    return bool(object_id) and set(object_id) == {"0"}


def _reference_matches(repository, ref, object_id):
    """Return whether ``ref`` still holds exactly ``object_id``.

    A zero object id means "the ref must not exist", so a missing ref is the
    unchanged state.  Any other id must equal the resolved ref target.
    """

    current = _resolve_head(repository, ref)
    if _is_zero_object_id(object_id):
        return current is None
    return current == object_id


def _compare_and_swap(
    repository, ref, new_id, old_id, error=PlaygroundHeadChangedError
):
    # ``update-ref`` with a zero ``new_id`` deletes the ref instead of moving it,
    # under the same "the old value must still be there" guard.
    arguments = (
        ("update-ref", "-d", ref, old_id)
        if _is_zero_object_id(new_id)
        else ("update-ref", ref, new_id, old_id)
    )
    completed = _run_git(arguments, cwd=repository.path)
    if completed.returncode != 0:
        # A failed update is only a lost race when the protected ref no longer
        # holds the expected object.  An unchanged ref means disk, lock, or
        # repository failure, which must not surface as concurrency.
        if _reference_matches(repository, ref, old_id):
            raise RepositoryError("could not update reference")
        raise error()


def publish_playground(repository, playground_id, bundle, metadata):
    """Build a commit from a validated bundle and CAS the Playground ref.

    ``bundle`` must be the exact immutable mapping returned by
    :func:`bundles.validate_bundle`; no directory, archive, or arbitrary
    mutable tree is accepted.  ``metadata`` is a trusted
    :class:`CommitMetadata`.  The function never overwrites a moved ref: a
    concurrent change surfaces as :class:`PlaygroundHeadChangedError`.
    """

    if not isinstance(repository, Repository):
        raise RepositoryError("repository handle required")
    _validate_commit_metadata(metadata)

    # Re-validates the identifier and returns only the namespaced Playground
    # ref; no caller-controlled ref string reaches Git.
    ref = repository.playground_ref(playground_id)

    files = _snapshot_bundle(bundle)
    tree = _bundle_tree(files)

    head = _resolve_head(repository, ref)
    head_tree = _commit_tree_id(repository, head) if head is not None else None

    new_tree_id = _write_bundle_tree(repository, tree)

    if head is not None and new_tree_id == head_tree:
        return PublishResult(
            playground_id=playground_id, commit_id=head, no_op=True
        )

    commit_id = _create_commit(repository, new_tree_id, head, metadata)
    old_id = head if head is not None else _zero_object_id(repository)
    _compare_and_swap(repository, ref, commit_id, old_id)
    return PublishResult(
        playground_id=playground_id, commit_id=commit_id, no_op=False
    )


def retire_playground(repository, playground_id):
    """Remove a Playground's published ref and report whether one was there.

    This is the whole of "students no longer see it": every read path resolves the
    ref first, so a Playground without one is simply not in the catalog.  Nothing
    else is touched — the commits stay in the object store (publishing the id
    again restores it), and the database row with the submissions that reference
    it is left alone, which is why retiring works even for a course students have
    already handed work in for.

    The delete is a compare-and-swap like a publish, so a concurrent publish of
    the same id surfaces as :class:`PlaygroundHeadChangedError` instead of being
    silently discarded.
    """

    if not isinstance(repository, Repository):
        raise RepositoryError("repository handle required")

    ref = repository.playground_ref(playground_id)
    head = _resolve_head(repository, ref)
    if head is None:
        return False

    _compare_and_swap(repository, ref, _zero_object_id(repository), head)
    return True


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
        ref = repository.playground_ref(playground_id)
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

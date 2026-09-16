"""Bootstrap and validate the private bare Git repository.

Only the Python standard library and the installed ``git`` executable are used.
This module deliberately exposes no generic ref helper: callers may only build
Playground or Work references through the two validated helpers on
:class:`Repository`.
"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from bundles import ALLOWED_PREFIXES, MANIFEST_PATH


DEFAULT_REPOSITORY_PATH = Path("data/picopg.git")
GIT_TIMEOUT_SECONDS = 30

# Git is authoritative for ref syntax, but reject obviously unsafe identifiers
# before starting a subprocess. The set matches the characters Git forbids in a
# ref component.
_REF_METACHARACTERS = frozenset(" ~^:?*[\\")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class RepositoryError(Exception):
    """Raised when the private repository cannot be opened or validated."""


class Repository:
    """Handle for one private bare Git repository."""

    def __init__(self, path):
        self.path = Path(path)

    def playground_ref(self, opaque_id):
        """Return the Playground ref for ``opaque_id``."""
        _validate_identifier(opaque_id, "playground id")
        ref = f"refs/heads/playgrounds/{opaque_id}"
        _check_ref_format(ref)
        return ref

    def work_ref(self, playground_id, student_id):
        """Return the Work ref for ``playground_id`` and ``student_id``."""
        _validate_identifier(playground_id, "playground id")
        _validate_identifier(student_id, "student id")
        ref = f"refs/heads/works/{playground_id}/{student_id}"
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
        return Repository(path)

    _initialize_bare_repository(path)
    if not _is_bare_repository(path):
        raise RepositoryError("repository was not initialized as bare")
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


def _check_ref_format(ref):
    completed = _run_git(("check-ref-format", ref))
    if completed.returncode != 0:
        raise RepositoryError("reference is not accepted by Git")


def _run_git(arguments, cwd=None, stdin=None, environment=None):
    effective_environment = _sanitized_git_environment()
    if environment is not None:
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

# Caller-provided variables that could redirect the repository, its object
# store, its configuration, or the commit identity are dropped before every
# Git invocation.  The explicit author/committer identity is supplied by
# :func:`publish_playground` itself after validation.
_REDIRECTING_GIT_ENVIRONMENT = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
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
    environment = {}
    for key, value in os.environ.items():
        if key in _REDIRECTING_GIT_ENVIRONMENT:
            continue
        if key.startswith("GIT_CONFIG_KEY_") or key.startswith(
            "GIT_CONFIG_VALUE_"
        ):
            continue
        environment[key] = value
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


def _is_allowed_bundle_path(path):
    if type(path) is not str or not path:
        return False
    if "\\" in path or _CONTROL_CHARACTERS.search(path):
        return False
    if path.startswith("/"):
        return False
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return False
    if path == MANIFEST_PATH:
        return True
    for prefix in ALLOWED_PREFIXES:
        if path.startswith(prefix) and len(path) > len(prefix):
            return True
    return False


def _snapshot_bundle(bundle):
    """Defensively copy a validated bundle into a sorted plain list.

    Only the exact read-only mapping type produced by
    :func:`bundles.validate_bundle` is accepted.  Paths and values are
    re-checked as exact plain ``str`` / ``bytes`` and against the task-7
    allowlist before any Git object is written.
    """

    if type(bundle) is not MappingProxyType:
        raise RepositoryError(
            "bundle must be the normalized read-only mapping"
        )
    files = []
    for path, data in bundle.items():
        if type(path) is not str or type(data) is not bytes:
            raise RepositoryError(
                "bundle entries must be plain str paths and bytes values"
            )
        if not _is_allowed_bundle_path(path):
            raise RepositoryError("bundle path is not allowed")
        files.append((path, data))
    if not any(path == MANIFEST_PATH for path, _ in files):
        raise RepositoryError("bundle is missing its manifest")
    files.sort(key=lambda item: item[0])
    return files


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


def _write_bundle_tree(repository, node):
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
            name.encode("utf-8"),
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


def _create_commit(repository, tree_id, parent, metadata):
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
        raise RepositoryError("could not create playground commit")
    return completed.stdout.strip().decode("ascii")


def _compare_and_swap(repository, ref, new_id, old_id):
    completed = _run_git(
        ("update-ref", ref, new_id, old_id), cwd=repository.path
    )
    if completed.returncode != 0:
        raise PlaygroundHeadChangedError()


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

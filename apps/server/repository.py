"""Bootstrap and validate the private bare Git repository.

Only the Python standard library and the installed ``git`` executable are used.
This module deliberately exposes no generic ref helper: callers may only build
Playground or Work references through the two validated helpers on
:class:`Repository`.
"""

import os
import re
import subprocess
from pathlib import Path


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


def _run_git(arguments, cwd=None):
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
            timeout=GIT_TIMEOUT_SECONDS,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RepositoryError("git command could not be executed") from error

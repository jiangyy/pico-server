"""Bounded filesystem permission helpers for the managed Server layout.

The managed layout is::

    data/                    0700
    data/app.db              0600
    data/picopg.git/         0700
    data/submissions/        0700

The enclosing ``0700`` directory is the principal protection for Git internals
and submission subtrees, so this module narrows a managed root and the files the
Server itself creates; it never recursively rewrites an administrator-provided
subtree and never changes an owner.  An existing path is only narrowed when it
is a real (non-symlink) path of the expected type owned by the effective user,
and a mode is never broadened.
"""

import os
import stat
from pathlib import Path


__all__ = [
    "DIRECTORY_MODE",
    "FILE_MODE",
    "PermissionPolicyError",
    "ensure_private_directory",
    "ensure_private_file",
]


DIRECTORY_MODE = 0o700
FILE_MODE = 0o600


class PermissionPolicyError(RuntimeError):
    """A managed path is the wrong type, wrong owner, or not narrowable."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _owned_by_effective_user(path):
    try:
        return os.stat(path).st_uid == os.geteuid()
    except (AttributeError, OSError):
        # ``geteuid`` is POSIX-only; where it is unavailable the caller keeps
        # ownership checks best-effort.
        return True


def _narrow(path, mode):
    try:
        current = stat.S_IMODE(os.stat(path).st_mode)
    except OSError as error:
        raise PermissionPolicyError("stat_failed") from error
    narrowed = current & mode
    if narrowed != current:
        try:
            os.chmod(path, narrowed)
        except OSError as error:
            raise PermissionPolicyError("chmod_failed") from error


def ensure_private_directory(path, mode=DIRECTORY_MODE, *, create=True):
    """Return ``path`` as a real private directory, or raise."""

    path = Path(path)
    if path.is_symlink():
        raise PermissionPolicyError("path_is_symlink")
    if path.exists():
        if not path.is_dir():
            raise PermissionPolicyError("not_a_directory")
        if not _owned_by_effective_user(path):
            raise PermissionPolicyError("wrong_owner")
        _narrow(path, mode)
        return path
    if not create:
        raise PermissionPolicyError("missing_directory")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=mode)
    except OSError as error:
        raise PermissionPolicyError("mkdir_failed") from error
    if not _owned_by_effective_user(path):
        raise PermissionPolicyError("wrong_owner")
    _narrow(path, mode)
    return path


def ensure_private_file(path, mode=FILE_MODE):
    """Return ``path`` as a real private regular file, or raise."""

    path = Path(path)
    if path.is_symlink():
        raise PermissionPolicyError("path_is_symlink")
    if not path.is_file():
        raise PermissionPolicyError("not_a_file")
    if not _owned_by_effective_user(path):
        raise PermissionPolicyError("wrong_owner")
    _narrow(path, mode)
    return path

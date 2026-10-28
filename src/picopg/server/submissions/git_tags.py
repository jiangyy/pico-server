"""Create immutable Git tags for the exact Revision named by a submission."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Mapping


class GitSubmissionTagWriter:
    def __init__(self, repositories: Mapping[str, Path]) -> None:
        if any(not isinstance(key, str) or not key for key in repositories):
            raise ValueError("playground IDs must be non-empty")
        self._repositories = dict(repositories)

    def create(
        self,
        *,
        idempotency_key: str,
        submission_id: str,
        playground_id: str,
        revision: object,
        source: str,
    ) -> str:
        del revision
        repository = self._repositories.get(playground_id)
        if repository is None:
            raise ValueError(f"no Git repository is configured for {playground_id!r}")
        if not idempotency_key or not submission_id:
            raise ValueError("submission identity must be non-empty")
        repository = repository.resolve(strict=True)
        tag_digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        tag = f"picopg/submissions/{tag_digest}"
        path = f"submissions/{hashlib.sha256(playground_id.encode('utf-8')).hexdigest()[:16]}/{tag_digest}.py"
        existing = self._run(repository, "rev-parse", "--verify", f"refs/tags/{tag}", check=False)
        if existing.returncode == 0:
            stored = self._run(repository, "show", f"{tag}:{path}").stdout
            if stored.decode("utf-8", errors="strict") != source:
                raise ValueError("submission tag already exists with different source")
            return tag

        parent = self._run(repository, "rev-parse", "--verify", "HEAD").stdout.decode().strip()
        blob = self._run(repository, "hash-object", "-w", "--stdin", input=source.encode("utf-8")).stdout.decode().strip()
        with tempfile.TemporaryDirectory(prefix="picopg-git-index-") as temporary:
            environment = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
            self._run(repository, "read-tree", parent, environment=environment)
            self._run(
                repository,
                "update-index",
                "--add",
                "--cacheinfo",
                "100644",
                blob,
                path,
                environment=environment,
            )
            tree = self._run(repository, "write-tree", environment=environment).stdout.decode().strip()
        message = f"PicoPG submission {submission_id}"
        identity = {
            "GIT_AUTHOR_NAME": "PicoPG Server",
            "GIT_AUTHOR_EMAIL": "server@picopg.invalid",
            "GIT_COMMITTER_NAME": "PicoPG Server",
            "GIT_COMMITTER_EMAIL": "server@picopg.invalid",
        }
        commit = self._run(
            repository,
            "commit-tree",
            tree,
            "-p",
            parent,
            "-m",
            message,
            environment=identity,
        ).stdout.decode().strip()
        created = self._run(repository, "tag", tag, commit, check=False)
        if created.returncode != 0:
            stored = self._run(repository, "show", f"{tag}:{path}").stdout
            if stored.decode("utf-8", errors="strict") != source:
                raise ValueError("submission tag was concurrently created with different source")
        return tag

    @staticmethod
    def _run(
        repository: Path,
        *arguments: str,
        input: bytes | None = None,
        environment: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        env = os.environ.copy()
        env.update(environment or {})
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        if check and result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"git {' '.join(arguments[:2])} failed: {message}")
        return result

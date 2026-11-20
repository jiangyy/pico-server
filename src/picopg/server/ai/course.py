from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

from picopg.server.ai.model import CourseAiError
from picopg.server.playgrounds.packages import (
    FilePackageStore,
    PackageError,
    PackageNotFound,
    validate_version_id,
)


class CourseSkillRepository:
    def __init__(self, root: Path, packages: FilePackageStore | None = None) -> None:
        self.root = root
        self.packages = packages

    def read(self, playground_id: str, version_id: str | None = None) -> dict[str, object]:
        packaged = self._packaged(playground_id, version_id)
        if packaged is not None:
            return _skill_value(packaged, playground_id)
        path = self.root / f"{playground_id}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise CourseAiError(f"course AI is not configured for {playground_id!r}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise CourseAiError(f"course AI skill is invalid for {playground_id!r}") from error
        return _skill_value(value, playground_id)

    def _packaged(self, playground_id: str, version_id: str | None) -> object | None:
        if self.packages is None or version_id is None:
            return None
        try:
            validate_version_id(version_id)
        except PackageError:
            return None
        try:
            payload = self.packages.read(playground_id, version_id)
        except PackageNotFound:
            return None
        except PackageError as error:
            raise CourseAiError(f"course package is invalid for {playground_id!r}") from error
        try:
            with zipfile.ZipFile(BytesIO(payload)) as archive:
                content = archive.read(f"course/{playground_id}.skill.json")
        except KeyError:
            return None
        except (OSError, RuntimeError, zipfile.BadZipFile) as error:
            raise CourseAiError(f"course package skill is unreadable for {playground_id!r}") from error
        try:
            return json.loads(content.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CourseAiError(f"course package skill is invalid for {playground_id!r}") from error


class OneSkill:
    """离线评测用：只有一份课程 skill，不管问哪门课都给它。"""

    def __init__(self, skill: dict[str, object]) -> None:
        self.skill = skill

    def read(self, playground_id: str, version_id: str | None = None) -> dict[str, object]:
        return self.skill


def _skill_value(value: object, playground_id: str) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("course") != playground_id:
        raise CourseAiError(f"course AI skill does not match {playground_id!r}")
    actions = value.get("actions")
    if not isinstance(actions, list) or any(not isinstance(item, str) for item in actions):
        raise CourseAiError(f"course AI skill actions are invalid for {playground_id!r}")
    return value

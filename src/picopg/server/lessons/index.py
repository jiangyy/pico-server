"""Lesson search: find where a concept is taught, from the outline each course package ships."""

from __future__ import annotations

import json
import re
import threading
import zipfile
from io import BytesIO
from typing import Protocol

from picopg.server.playgrounds.packages import PackageError, PackageNotFound
from picopg.server.playgrounds.releases import PlaygroundRelease

# 提问里常见、却不指向任何内容的双字词。
STOP = frozenset({"我想", "想学", "怎么", "什么", "如何", "为什么", "一下", "可以", "这个", "那个", "学习", "知道", "请问", "是什", "么是"})
MAX_RESULTS = 10


class Catalog(Protocol):
    def catalog(self) -> tuple[PlaygroundRelease, ...]: ...

    def package(self, playground_id: str, requested_version: str) -> tuple[str, bytes]: ...


class LessonIndex:
    def __init__(self, playgrounds: Catalog) -> None:
        self.playgrounds = playgrounds
        self.outlines: dict[tuple[str, str], dict[str, object] | None] = {}
        self.lock = threading.Lock()

    def search(self, query: str, limit: int = 5) -> list[dict[str, object]]:
        query = query.strip()
        if not query:
            return []
        terms = _terms(query)
        lowered = query.lower()
        found: list[tuple[int, int, dict[str, object]]] = []
        for order, release in enumerate(self.playgrounds.catalog()):
            outline = self._outline(release)
            if outline is None:
                continue
            found.extend((score, order, hit) for score, hit in _hits(release, outline, lowered, terms))
        found.sort(key=lambda item: (-item[0], item[1], item[2]["lecture"]["id"], item[2]["step"]))
        results: list[dict[str, object]] = []
        per_course: dict[str, int] = {}
        for _, _, hit in found:
            course = hit["playgroundId"]
            # 每门课最多两处，结果不被一门长课占满。
            if per_course.get(course, 0) >= 2:
                continue
            per_course[course] = per_course.get(course, 0) + 1
            results.append(hit)
            if len(results) >= min(max(limit, 1), MAX_RESULTS):
                break
        return results

    def _outline(self, release: PlaygroundRelease) -> dict[str, object] | None:
        key = (release.playground_id, release.version_id)
        with self.lock:
            if key in self.outlines:
                return self.outlines[key]
        try:
            _, payload = self.playgrounds.package(release.playground_id, release.version_id)
            with zipfile.ZipFile(BytesIO(payload)) as archive:
                value = json.loads(archive.read(f"course/{release.playground_id}.outline.json").decode("utf-8"))
            outline = value if isinstance(value, dict) and value.get("version") == 1 else None
        except (KeyError, PackageNotFound, PackageError, OSError, RuntimeError, zipfile.BadZipFile, ValueError):
            outline = None
        with self.lock:
            self.outlines[key] = outline
        return outline


def _terms(query: str) -> set[str]:
    """中文按相邻两字切，英文与数字按词；用来与步骤的标题和讲解文字比对。"""
    lowered = query.lower()
    words = {word for word in re.findall(r"[a-z_][a-z0-9_]+", lowered)}
    han = re.findall(r"[一-鿿]+", query)
    pairs = {run[index:index + 2] for run in han for index in range(len(run) - 1)}
    return (words | pairs) - STOP


def _hits(release: PlaygroundRelease, outline: dict[str, object], lowered: str, terms: set[str]):
    concepts = [item for item in outline.get("concepts", []) if isinstance(item, dict)]
    named = {
        str(item.get("id")): str(item.get("label"))
        for item in concepts
        if any(isinstance(word, str) and word and word.lower() in lowered for word in [item.get("label"), *item.get("aliases", [])])
    }
    course_concepts = {str(item) for item in outline.get("courseConcepts", [])}
    title = str(outline.get("title") or release.title or release.playground_id)
    title_named = title.lower() in lowered or release.playground_id.lower() in lowered
    for lecture in outline.get("lectures", []):
        if not isinstance(lecture, dict):
            continue
        for step in lecture.get("steps", []):
            if not isinstance(step, dict):
                continue
            step_concepts = {str(item) for item in step.get("concepts", [])}
            matched = [named[item] for item in sorted(step_concepts & named.keys())]
            text_title = str(step.get("title") or "")
            text_say = str(step.get("say") or "")
            in_title = sorted(term for term in terms if term in text_title.lower())
            in_say = sorted(term for term in terms if term in text_say.lower() and term not in in_title)
            score = 3 * len(matched) + 2 * len(in_title) + len(in_say)
            if not matched and not step_concepts and course_concepts & named.keys():
                score += 1
            if title_named:
                score += 2
            if score == 0:
                continue
            reasons = []
            if matched:
                reasons.append("讲到" + "、".join(matched))
            if in_title or in_say:
                reasons.append("提到“" + "”“".join((in_title + in_say)[:3]) + "”")
            if title_named:
                reasons.append(f"在「{title}」里")
            yield score, {
                "playgroundId": release.playground_id,
                "versionId": release.version_id,
                "courseTitle": title,
                "lecture": {"id": str(lecture.get("id")), "title": str(lecture.get("title"))},
                "step": int(step.get("step", 0)),
                "stepTitle": text_title or None,
                "summary": text_say[:80],
                "concepts": [str(item.get("label")) for item in concepts if str(item.get("id")) in step_concepts],
                "reason": "；".join(reasons),
            }

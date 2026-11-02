from __future__ import annotations

import http.client
import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from urllib.parse import quote

from picopg.replica.file_store import FileReplicaLogStore
from picopg.server.app import PicoPGHttpServer
from picopg.server.auth.model import PrincipalSession
from picopg.server.lessons import LessonIndex
from picopg.server.playgrounds.packages import PackageNotFound
from picopg.server.playgrounds.releases import PlaygroundRelease

HANOI = {
    "version": 1, "course": "hanoi", "title": "汉诺塔",
    "concepts": [{"id": "recursion", "label": "递归", "aliases": ["递归出口", "调用栈"]}],
    "courseConcepts": ["recursion"],
    "lectures": [{"id": "hanoi", "title": "单向汉诺塔", "steps": [
        {"step": 0, "title": "函数的承诺", "say": "先相信 hanoi 能把 n 个盘子搬过去。", "concepts": ["recursion"], "prediction": None},
        {"step": 1, "title": "递归出口", "say": "n 等于 1 时直接走，不再调用自己。", "concepts": ["recursion"], "prediction": None},
        {"step": 2, "title": "while 单步", "say": "用 while 一格一格走到目标柱。", "concepts": [], "prediction": None},
    ]}],
}
DICTS = {
    "version": 1, "course": "data-structures", "title": "数据结构",
    "concepts": [{"id": "dict", "label": "字典", "aliases": ["键值", "key"]}],
    "courseConcepts": ["list", "dict"],
    "lectures": [{"id": "ds", "title": "一个列表，几个名字", "steps": [
        {"step": 0, "title": "字典里存的是引用", "say": "字典按键找值。", "concepts": ["dict"], "prediction": None},
    ]}],
}


def package(course: str, outline: dict | None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"course/{course}.js", "export {}")
        if outline is not None:
            archive.writestr(f"course/{course}.outline.json", json.dumps(outline, ensure_ascii=False))
    return buffer.getvalue()


class FakeCatalog:
    def __init__(self, packages: dict[str, bytes]) -> None:
        self.packages = packages
        self.reads = 0

    def catalog(self):
        return tuple(PlaygroundRelease(course, f"sha256:{course}", "application/zip", None) for course in sorted(self.packages))

    def package(self, playground_id, version_id):
        self.reads += 1
        if playground_id not in self.packages:
            raise PackageNotFound("missing")
        return "application/zip", self.packages[playground_id]


class LessonIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = FakeCatalog({"hanoi": package("hanoi", HANOI), "data-structures": package("data-structures", DICTS), "hello": package("hello", None)})
        self.index = LessonIndex(self.catalog)

    def test_a_concept_alias_finds_the_step_that_teaches_it(self) -> None:
        results = self.index.search("我想学递归出口是什么")
        assert results[0]["playgroundId"] == "hanoi" and results[0]["step"] == 1
        assert results[0]["lecture"] == {"id": "hanoi", "title": "单向汉诺塔"}
        assert "递归" in results[0]["reason"]
        assert all(item["playgroundId"] == "hanoi" for item in results) and len(results) == 2, "at most two places per course"

    def test_words_in_the_step_text_count_and_courses_without_outlines_are_skipped(self) -> None:
        assert self.index.search("字典怎么按键找值")[0]["playgroundId"] == "data-structures"
        assert self.index.search("while")[0]["step"] == 2
        assert self.index.search("今天天气") == []
        assert self.index.search("   ") == []

    def test_the_newest_knowledge_map_shipped_with_any_course_wins(self) -> None:
        assert self.index.knowledge() is None, "packages published before maps were shipped carry none"
        catalog = FakeCatalog({
            "hanoi": package("hanoi", {**HANOI, "knowledge": {"version": 2, "concepts": []}}),
            "data-structures": package("data-structures", {**DICTS, "knowledge": {"version": 3, "concepts": []}}),
        })
        assert LessonIndex(catalog).knowledge()["version"] == 3

    def test_outlines_are_read_once_per_version(self) -> None:
        self.index.search("递归")
        reads = self.catalog.reads
        self.index.search("字典")
        assert self.catalog.reads == reads


class LessonRouteTest(unittest.TestCase):
    def test_signed_in_students_search_the_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = PrincipalSession.create("student", "device", "student-token", {}, ["playgrounds"])
            server = PicoPGHttpServer(("127.0.0.1", 0), FileReplicaLogStore(root / "replica"), "operator-token", root / "packages",
                                      principal_sessions=[session])
            server.lessons = LessonIndex(FakeCatalog({"hanoi": package("hanoi", HANOI)}))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(*server.server_address)
            try:
                def get(token: str, query: str):
                    connection.request("GET", f"/api/v1/lessons?q={quote(query)}", headers={"Authorization": f"Bearer {token}"})
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                status, body = get("student-token", "递归")
                assert status == 200 and body["results"][0]["playgroundId"] == "hanoi"
                status, body = get("wrong", "递归")
                assert status == 401 and body["error"]["code"] == "auth.required"
                status, body = get("student-token", "长" * 201)
                assert status == 400
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join()

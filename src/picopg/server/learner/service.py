"""学生画像的增量计算：每个学生一个累加器，记住每份证据日志读到哪里，之后只读新增的记录。

累加器的大小有上限（见 profile.py）；同时留在内存里的学生也有上限，最久没用到的先放掉，下次用到时从头重建。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from picopg.replica import Position, ReplicaLogStore
from picopg.server.common.ids import encode_component
from picopg.server.learner.profile import Learner

PAGE = 128


@dataclass
class _Entry:
    knowledge: object
    learner: Learner
    read: dict[str, Position] = field(default_factory=dict)
    profile: dict[str, object] | None = None
    # 只锁这个学生：读证据、喂累加器都在它里面，别的学生的画像不用排队。
    lock: threading.Lock = field(default_factory=threading.Lock)


class LearnerProfiles:
    def __init__(
        self, store: ReplicaLogStore, knowledge: Callable[[], Mapping[str, object] | None], students: int = 2000,
    ) -> None:
        self.store = store
        self.knowledge = knowledge
        self.students = students
        self.entries: OrderedDict[str, _Entry] = OrderedDict()
        self.lock = threading.Lock()

    def profile(self, principal_id: str) -> dict[str, object]:
        entry = self._entry(principal_id)
        with entry.lock:
            self._catch_up(principal_id, entry)
            if entry.profile is None:
                entry.profile = entry.learner.profile()
            return entry.profile

    def activity(self, principal_id: str, limit: int = 100) -> list[dict[str, object]]:
        entry = self._entry(principal_id)
        with entry.lock:
            return self._catch_up(principal_id, entry).learner.recent(limit)

    def _entry(self, principal_id: str) -> _Entry:
        """表上的锁只管取出或换掉这个学生的累加器，很快；计算在学生自己的锁里做。"""
        knowledge = self.knowledge()
        version = None if knowledge is None else knowledge.get("version")
        with self.lock:
            entry = self.entries.get(principal_id)
            # 地图换了版本：概念的换算变了，从头重算。
            if entry is None or entry.knowledge != version:
                entry = _Entry(version, Learner(knowledge))
                self.entries[principal_id] = entry
            self.entries.move_to_end(principal_id)
            while len(self.entries) > self.students:
                self.entries.popitem(last=False)
            return entry

    def _catch_up(self, principal_id: str, entry: _Entry) -> _Entry:
        for log in self._logs(principal_id):
            after = entry.read.get(log)
            while True:
                page = self.store.read(log, after, PAGE)
                for record in page.records:
                    data = record.to_value()["data"]
                    if isinstance(data, dict):
                        entry.learner.feed(data)
                    after = record.position
                    entry.profile = None
                if not page.has_more or not page.records:
                    break
            if after is not None:
                entry.read[log] = after
        return entry

    def _logs(self, principal_id: str) -> list[str]:
        # 证据日志按设备分：principal/<学生>/analysis/<设备>/evidence。
        prefix = f"principal/{encode_component(principal_id, 'principalId')}/analysis/"
        return sorted(log for log in self.store.log_ids() if log.startswith(prefix) and log.endswith("/evidence"))

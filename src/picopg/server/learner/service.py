"""按需算出学生画像：读这个学生所有设备的证据日志，日志与地图没变时直接用上次的结果。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping

from picopg.replica import ReplicaLogStore
from picopg.server.common.ids import encode_component
from picopg.server.common.reads import read_all
from picopg.server.learner.profile import activity_of, profile_of


class LearnerProfiles:
    def __init__(self, store: ReplicaLogStore, knowledge: Callable[[], Mapping[str, object] | None]) -> None:
        self.store = store
        self.knowledge = knowledge
        self.cache: dict[str, tuple[object, dict[str, object]]] = {}
        self.lock = threading.Lock()

    def profile(self, principal_id: str) -> dict[str, object]:
        logs = self._logs(principal_id)
        knowledge = self.knowledge()
        key = (tuple((log, self.store.head(log)) for log in logs), None if knowledge is None else knowledge.get("version"))
        with self.lock:
            cached = self.cache.get(principal_id)
        if cached is not None and cached[0] == key:
            return cached[1]
        profile = profile_of(self._evidence(logs), knowledge)
        with self.lock:
            self.cache[principal_id] = (key, profile)
        return profile

    def activity(self, principal_id: str, limit: int = 100) -> list[dict[str, object]]:
        return activity_of(self._evidence(self._logs(principal_id)), limit)

    def _logs(self, principal_id: str) -> list[str]:
        # 证据日志按设备分：principal/<学生>/analysis/<设备>/evidence。
        prefix = f"principal/{encode_component(principal_id, 'principalId')}/analysis/"
        return sorted(log for log in self.store.log_ids() if log.startswith(prefix) and log.endswith("/evidence"))

    def _evidence(self, logs: list[str]) -> list[dict[str, object]]:
        records = [
            (index, record.to_value()["data"])
            for log in logs for index, record in enumerate(read_all(self.store, log))
        ]
        # 多台设备的证据按设备时间合并；没有时间的旧记录保持在各自日志里的先后。
        records.sort(key=lambda item: (str(item[1].get("recordedAt", "")) if isinstance(item[1], dict) else "", item[0]))
        return [data for _, data in records if isinstance(data, dict)]

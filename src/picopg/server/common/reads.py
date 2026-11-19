from __future__ import annotations

from picopg.replica import Position, ReplicaLogStore, ReplicatedRecord


_PAGE_SIZE = 128


class PaginationStalled(ValueError):
    pass


def read_all(store: ReplicaLogStore, log_id: str) -> tuple[ReplicatedRecord, ...]:
    return read_after(store, log_id, None)


def read_after(
    store: ReplicaLogStore, log_id: str, after: Position | None
) -> tuple[ReplicatedRecord, ...]:
    """after 之后新追加的记录（after 为 None 时是整个日志）。"""
    records: list[ReplicatedRecord] = []
    while True:
        page = store.read(log_id, after, _PAGE_SIZE)
        records.extend(page.records)
        if not page.has_more:
            return tuple(records)
        if not page.records:
            raise PaginationStalled(
                f"{log_id} returned an empty continuation page"
            )
        after = page.records[-1].position


def find_record(
    store: ReplicaLogStore, position: Position
) -> ReplicatedRecord | None:
    return store.record(position)

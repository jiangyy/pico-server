from __future__ import annotations

from picopg.api import Position, ReplicaLogStore, ReplicatedRecord


_PAGE_SIZE = 128


class PaginationStalled(ValueError):
    pass


def read_all(store: ReplicaLogStore, log_id: str) -> tuple[ReplicatedRecord, ...]:
    records: list[ReplicatedRecord] = []
    after: Position | None = None
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
    if not store.has(position):
        return None
    return next(
        (
            record
            for record in read_all(store, position.log_id)
            if record.id == position.record_id
        ),
        None,
    )

from __future__ import annotations

import argparse
import math
import sys
import tempfile
from pathlib import Path
from time import perf_counter


ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "src"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from picopg.api import Position, ReplicatedRecord, record_id_of  # noqa: E402
from picopg.impl.file_replica_log import FileReplicaLogStore  # noqa: E402


def record(log_id: str, parent: Position | None, index: int, payload: str) -> ReplicatedRecord:
    data = {"index": index, "payload": payload}
    return ReplicatedRecord(
        record_id_of(log_id, parent, (), data),
        log_id,
        parent,
        (),
        data,
    )


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * ratio) - 1)]


def measure(count: int, payload_bytes: int, samples: int) -> tuple[int, float, float, float]:
    with tempfile.TemporaryDirectory(prefix="picopg-benchmark-") as raw:
        root = Path(raw)
        store = FileReplicaLogStore(root)
        log_id = "benchmark"
        payload = "x" * payload_bytes
        records: list[ReplicatedRecord] = []
        parent = None
        for index in range(count):
            item = record(log_id, parent, index, payload)
            records.append(item)
            parent = item.position
        assert store.append(log_id, None, tuple(records)).kind == "accepted"

        durations: list[float] = []
        for index in range(count, count + samples):
            item = record(log_id, parent, index, payload)
            started = perf_counter()
            assert store.append(log_id, parent, (item,)).kind == "accepted"
            durations.append((perf_counter() - started) * 1_000)
            parent = item.position

        path = root / "replica-log.json"
        started = perf_counter()
        reopened = FileReplicaLogStore(root)
        load_ms = (perf_counter() - started) * 1_000
        assert reopened.head(log_id) == parent
        return (
            path.stat().st_size,
            percentile(durations, 0.50),
            percentile(durations, 0.95),
            load_ms,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark FileReplicaLogStore scaling")
    parser.add_argument("--records", default="100,1000,5000")
    parser.add_argument("--payload-bytes", type=int, default=1024)
    parser.add_argument("--samples", type=int, default=15)
    args = parser.parse_args()
    counts = [int(value) for value in args.records.split(",")]
    if any(value < 1 for value in counts) or args.payload_bytes < 0 or args.samples < 1:
        parser.error("records and samples must be positive; payload bytes must be non-negative")

    print("records,file_mib,append_p50_ms,append_p95_ms,cold_load_ms")
    for count in counts:
        size, p50, p95, load = measure(count, args.payload_bytes, args.samples)
        print(f"{count},{size / 1024 / 1024:.3f},{p50:.3f},{p95:.3f},{load:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

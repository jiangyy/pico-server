from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for _entry in (_ROOT, _SRC):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from apps.server.protocol import ReplicaHttpServer  # noqa: E402
from picopg.impl.file_replica_log import FileReplicaLogStore  # noqa: E402


logger = logging.getLogger("picopg.server")


def build_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PicoPG replica server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--token")
    return parser.parse_args(argv)


def load_token(data_dir: Path, argument: str | None) -> str:
    configured = os.environ.get("PICOPG_REPLICA_TOKEN") or argument
    if configured:
        return configured
    path = data_dir / "replica.token"
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError(f"replica token is empty: {path}")
        return token
    data_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(token + "\n")
        output.flush()
        os.fsync(output.fileno())
    logger.info("generated replica token at %s", path)
    return token


def build_server(
    data_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8001,
    token: str,
) -> ReplicaHttpServer:
    store = FileReplicaLogStore(data_dir / "replica")
    return ReplicaHttpServer((host, port), store, token)


def main(argv: list[str] | None = None) -> int:
    arguments = build_arguments(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = load_token(arguments.data, arguments.token)
    server = build_server(
        arguments.data,
        host=arguments.host,
        port=arguments.port,
        token=token,
    )
    host, port = server.server_address
    logger.info("replica server listening on http://%s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

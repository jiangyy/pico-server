from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for _entry in (_ROOT, _SRC):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from apps.server.authentication.model import PrincipalAccount  # noqa: E402
from apps.server.main import build_server  # noqa: E402
from smoke import run  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as raw_dir:
        server = build_server(
            Path(raw_dir), host="127.0.0.1", port=0, token="operator-token",
            principal_accounts=[PrincipalAccount.create(
                "student", "access-code", ("playgrounds", "drafts", "submissions")
            )],
            secure_cookies=False,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        try:
            result = run(
                f"http://{host}:{port}", "access-code", "operator-token",
                allow_http=True, check_static=False,
            )
            assert result.principal_id == "student"
            assert result.replica_id
            assert result.submissions == 0
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("Session, SSE, operator and logout smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

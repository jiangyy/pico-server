from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for _entry in (_ROOT, _SRC):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from picopg.server.auth.model import PrincipalAccount  # noqa: E402
from picopg.server.app import build_server  # noqa: E402
from picopg.server.submissions.git_tags import GitSubmissionTagWriter  # noqa: E402
from deploy.smoke import prepare, verify  # noqa: E402


def _package() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("picopg.json", json.dumps({
            "schemaVersion": 1,
            "runtime": "python",
            "module": "course/smoke.js",
            "title": "Deployment smoke",
        }))
        archive.writestr("course/index.html", "smoke")
        archive.writestr("course/smoke.js", "export const Root = () => null;")
        archive.writestr("starter/main.py", "print('starter')\n")
    return output.getvalue()


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _repository(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-q")
    (path / "README.md").write_text("smoke\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(
        path,
        "-c",
        "user.name=PicoPG Test",
        "-c",
        "user.email=test@picopg.invalid",
        "commit",
        "-q",
        "-m",
        "Initialize smoke repository",
    )


def _start(data: Path, repository: Path):
    server = build_server(
        data,
        host="127.0.0.1",
        port=0,
        token="operator-token",
        principal_accounts=[PrincipalAccount.create(
            "student", "access-code", ("playgrounds", "drafts", "submissions", "analysis")
        )],
        secure_cookies=False,
        submission_tag_writer=GitSubmissionTagWriter({"smoke": repository}),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def _stop(server, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def main() -> int:
    with tempfile.TemporaryDirectory() as raw_dir:
        root = Path(raw_dir)
        data = root / "data"
        repository = root / "submission-repository"
        _repository(repository)

        server, thread, base_url = _start(data, repository)
        try:
            published = server.playgrounds.publish(
                "smoke", None, "application/zip", _package()
            )
            assert published.status == "accepted"
            state, prepared = prepare(
                base_url,
                "access-code",
                "operator-token",
                "smoke",
                allow_http=True,
                check_static=False,
            )
            assert prepared.principal_id == "student"
            assert prepared.tag == state.tag
            _git(repository, "rev-parse", "--verify", f"refs/tags/{state.tag}")
            first_replica = state.replica_id
            state, prepared = prepare(
                base_url,
                "access-code",
                "operator-token",
                "smoke",
                allow_http=True,
                check_static=False,
            )
            assert state.replica_id != first_replica
            assert prepared.tag == state.tag
            _git(repository, "rev-parse", "--verify", f"refs/tags/{state.tag}")
        finally:
            _stop(server, thread)

        server, thread, base_url = _start(data, repository)
        try:
            verified = verify(
                base_url,
                "operator-token",
                state,
                allow_http=True,
                check_static=False,
            )
            assert verified.principal_id == "student"
            assert verified.replica_id == state.replica_id
            assert verified.tag == state.tag
            assert verified.submissions >= 1
        finally:
            _stop(server, thread)

    print("mutation, SSE, worker, Git tag, comment and restart smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

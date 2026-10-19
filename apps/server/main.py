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

from apps.server.application import PicoPGHttpServer  # noqa: E402
from apps.server.authentication.model import PrincipalAccount, PrincipalSession  # noqa: E402
from apps.server.learning_analysis.openai_provider import (  # noqa: E402
    OpenAIStructuredLearningProvider,
)
from apps.server.learning_analysis.pipeline import LearningFeedbackAnalyzer  # noqa: E402
from apps.server.learning_analysis.worker import LearningAnalyzer  # noqa: E402
from apps.server.submission.git_tags import GitSubmissionTagWriter  # noqa: E402
from apps.server.submission.worker import SubmissionTagWriter  # noqa: E402
from apps.server.authentication.registry import FileSessionRegistry  # noqa: E402
from picopg.impl.file_replica_log import FileReplicaLogStore  # noqa: E402


logger = logging.getLogger("picopg.server")


def build_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PicoPG replica server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--token")
    parser.add_argument(
        "--binding",
        action="append",
        default=[],
        metavar="LOG_ID=ACCESS",
        help="Expose LOG_ID with ACCESS in {read, append}; repeat per Log.",
    )
    parser.add_argument(
        "--binding-config",
        type=Path,
        help="Path to a JSON file of [[logId, access], ...] bindings.",
    )
    parser.add_argument(
        "--principal-config",
        type=Path,
        help="JSON array of principal sessions with grants and/or semantic capabilities.",
    )
    parser.add_argument(
        "--submission-repositories",
        type=Path,
        help="JSON object mapping playground IDs to Git repository paths.",
    )
    parser.add_argument(
        "--insecure-cookies",
        action="store_true",
        help="Allow browser Session cookies over local HTTP; never use in production.",
    )
    parser.add_argument(
        "--learning-model",
        default=os.environ.get("PICOPG_LEARNING_MODEL"),
        help="Enable learning analysis with this OpenAI model (or PICOPG_LEARNING_MODEL).",
    )
    parser.add_argument(
        "--learning-api-base",
        default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        help="OpenAI-compatible API base URL (or OPENAI_BASE_URL).",
    )
    return parser.parse_args(argv)


def _parse_pair(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError(f"--binding expects LOG_ID=ACCESS, got {value!r}")
    log_id, _, access = value.partition("=")
    log_id = log_id.strip()
    access = access.strip()
    if not log_id:
        raise ValueError(f"--binding expects non-empty logId in {value!r}")
    if access not in ("read", "append"):
        raise ValueError(
            f"--binding access for {log_id!r} must be 'read' or 'append', got {access!r}"
        )
    return log_id, access


def load_bindings(
    pairs: list[str], config_path: Path | None
) -> list[tuple[str, str]]:
    bindings = [_parse_pair(item) for item in pairs]
    if config_path is None:
        return bindings
    import json
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("binding config must be a JSON array of [logId, access] pairs")
    for entry in raw:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not isinstance(entry[0], str)
            or not entry[0]
            or entry[1] not in ("read", "append")
        ):
            raise ValueError(
                f"binding config entry must be [logId, 'read'|'append'], got {entry!r}"
            )
        bindings.append((entry[0], entry[1]))
    return bindings


def load_principals(path: Path | None) -> tuple[list[PrincipalSession], list[PrincipalAccount]]:
    if path is None:
        return [], []
    import json
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("principal config must be a JSON array")
    sessions: list[PrincipalSession] = []
    accounts: list[PrincipalAccount] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("principal entry must be an object")
        if "accessCode" in item:
            required = {"principalId", "accessCode"}
            allowed = required | {"capabilities"}
            if not required.issubset(item) or not set(item).issubset(allowed):
                raise ValueError("account entry must contain principalId and accessCode")
            capabilities = item.get("capabilities", [])
            if not isinstance(capabilities, list) or any(not isinstance(value, str) for value in capabilities):
                raise ValueError("principal capabilities must be an array of strings")
            accounts.append(PrincipalAccount.create(
                item["principalId"], item["accessCode"], capabilities,
            ))
            continue
        required = {"principalId", "replicaId", "token"}
        allowed = required | {"grants", "capabilities"}
        if not required.issubset(item) or not set(item).issubset(allowed):
            raise ValueError("principal entry must contain principalId, replicaId and token")
        grants = item.get("grants", [])
        if not isinstance(grants, list):
            raise ValueError("principal grants must be an array")
        mapped: dict[str, str] = {}
        for grant in grants:
            if (not isinstance(grant, list) or len(grant) != 2
                    or not isinstance(grant[0], str) or not grant[0]
                    or grant[1] not in ("read", "append")):
                raise ValueError("principal grant must be [logId, 'read'|'append']")
            if grant[0] in mapped:
                raise ValueError(f"duplicate principal grant for {grant[0]!r}")
            mapped[grant[0]] = grant[1]
        capabilities = item.get("capabilities", [])
        if not isinstance(capabilities, list) or any(not isinstance(value, str) for value in capabilities):
            raise ValueError("principal capabilities must be an array of strings")
        sessions.append(PrincipalSession.create(
            item["principalId"], item["replicaId"], item["token"], mapped, capabilities,
        ))
    if len({session.token for session in sessions}) != len(sessions):
        raise ValueError("principal tokens must be unique")
    if len({(session.principal_id, session.replica_id) for session in sessions}) != len(sessions):
        raise ValueError("principal Replica identities must be unique")
    if len({account.principal_id for account in accounts}) != len(accounts):
        raise ValueError("principal accounts must be unique")
    if len({account.access_code for account in accounts}) != len(accounts):
        raise ValueError("principal access codes must be unique")
    return sessions, accounts


def load_principal_sessions(path: Path | None) -> list[PrincipalSession]:
    sessions, accounts = load_principals(path)
    if accounts:
        raise ValueError("principal config contains bootstrap accounts")
    return sessions


def load_submission_tag_writer(path: Path | None) -> SubmissionTagWriter | None:
    if path is None:
        return None
    import json
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or any(
        not isinstance(playground_id, str) or not playground_id
        or not isinstance(repository, str) or not repository
        for playground_id, repository in raw.items()
    ):
        raise ValueError("submission repository config must map non-empty playground IDs to paths")
    repositories = {
        playground_id: (path.parent / repository).resolve()
        if not Path(repository).is_absolute()
        else Path(repository)
        for playground_id, repository in raw.items()
    }
    return GitSubmissionTagWriter(repositories)


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


def load_learning_analyzer(
    model: str | None,
    *,
    api_key: str | None,
    api_base: str,
) -> LearningAnalyzer | None:
    if model is None:
        return None
    if not model.strip():
        raise ValueError("learning model must be non-empty")
    if api_key is None or not api_key.strip():
        raise ValueError("OPENAI_API_KEY is required when learning analysis is enabled")
    provider = OpenAIStructuredLearningProvider(
        api_key=api_key,
        model=model,
        base_url=api_base,
    )
    return LearningFeedbackAnalyzer(provider)


def build_server(
    data_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8001,
    token: str,
    bindings: list[tuple[str, str]] | None = None,
    principal_sessions: list[PrincipalSession] | None = None,
    principal_accounts: list[PrincipalAccount] | None = None,
    secure_cookies: bool = True,
    submission_tag_writer: SubmissionTagWriter | None = None,
    learning_analyzer: LearningAnalyzer | None = None,
) -> PicoPGHttpServer:
    store = FileReplicaLogStore(data_dir / "replica")
    return PicoPGHttpServer(
        (host, port),
        store,
        token,
        data_dir / "packages",
        bindings=bindings,
        principal_sessions=principal_sessions or (),
        principal_accounts=principal_accounts or (),
        session_registry=FileSessionRegistry(data_dir / "auth" / "sessions.json"),
        secure_cookies=secure_cookies,
        submission_tag_writer=submission_tag_writer,
        learning_analyzer=learning_analyzer,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = build_arguments(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = load_token(arguments.data, arguments.token)
    try:
        bindings = load_bindings(arguments.binding, arguments.binding_config)
        principal_sessions, principal_accounts = load_principals(arguments.principal_config)
        submission_tag_writer = load_submission_tag_writer(arguments.submission_repositories)
        learning_analyzer = load_learning_analyzer(
            arguments.learning_model,
            api_key=os.environ.get("OPENAI_API_KEY"),
            api_base=arguments.learning_api_base,
        )
    except ValueError as error:
        logger.error("invalid server configuration: %s", error)
        return 2
    server = build_server(
        arguments.data,
        host=arguments.host,
        port=arguments.port,
        token=token,
        bindings=bindings,
        principal_sessions=principal_sessions,
        principal_accounts=principal_accounts,
        secure_cookies=not arguments.insecure_cookies,
        submission_tag_writer=submission_tag_writer,
        learning_analyzer=learning_analyzer,
    )
    host, port = server.server_address
    logger.info(
        "replica server listening on http://%s:%s with %d Log binding(s)",
        host, port, len(server.bindings),
    )
    if learning_analyzer is not None:
        logger.info("learning analysis enabled with model %s", arguments.learning_model)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

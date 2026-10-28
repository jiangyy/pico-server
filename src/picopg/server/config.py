"""命令行与环境变量之外的服务器配置：绑定、账号、提交仓库、令牌与 AI。"""
from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path

from picopg.server.ai import (
    CourseAgent,
    CourseAgentService,
    CourseAi,
    CourseAiService,
    CourseSkillRepository,
    OpenAiCompatibleChatModel,
)
from picopg.server.auth.model import PrincipalAccount, PrincipalSession
from picopg.server.submissions.git_tags import GitSubmissionTagWriter
from picopg.server.submissions.worker import SubmissionTagWriter


logger = logging.getLogger("picopg.server")
SKILLS = Path(__file__).resolve().parent / "ai" / "skills"


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


def load_course_ai() -> CourseAi | None:
    base_url = os.environ.get("PICOPG_AI_BASE_URL")
    model = os.environ.get("PICOPG_AI_MODEL")
    if base_url is None and model is None:
        return None
    if not base_url or not model:
        raise ValueError("PICOPG_AI_BASE_URL and PICOPG_AI_MODEL must be set together")
    chat = OpenAiCompatibleChatModel(base_url, model, os.environ.get("PICOPG_AI_API_KEY"))
    return CourseAiService(chat, CourseSkillRepository(SKILLS))


def load_course_agent() -> CourseAgent | None:
    """与课程 AI 同一个模型；PICOPG_AI_EFFORT 可选（如 low），推理模型据此少想一点、答得更快。"""
    base_url = os.environ.get("PICOPG_AI_BASE_URL")
    model = os.environ.get("PICOPG_AI_MODEL")
    if not base_url or not model:
        return None
    chat = OpenAiCompatibleChatModel(
        base_url, model, os.environ.get("PICOPG_AI_API_KEY"), os.environ.get("PICOPG_AI_EFFORT") or None,
    )
    return CourseAgentService(chat, CourseSkillRepository(SKILLS))

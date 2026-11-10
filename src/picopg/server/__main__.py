from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from picopg.server.ai.access import AiAccess
from picopg.server.app import build_server
from picopg.server.client_log import write_client_log_to
from picopg.server.config import (
    load_ai_budget,
    load_bindings,
    load_course_agent,
    load_course_ai,
    load_course_diagnose,
    load_course_follow,
    load_course_hint,
    load_principals,
    load_submission_tag_writer,
    load_token,
)


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = build_arguments(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    write_client_log_to(arguments.data / "logs" / "client.log")
    token = load_token(arguments.data, arguments.token)
    try:
        bindings = load_bindings(arguments.binding, arguments.binding_config)
        principal_sessions, principal_accounts = load_principals(arguments.principal_config)
        submission_tag_writer = load_submission_tag_writer(arguments.submission_repositories)
        ai_budget = load_ai_budget()
        course_ai = load_course_ai(ai_budget, arguments.data / "packages")
        course_agent = load_course_agent(ai_budget, arguments.data / "packages")
        course_hint = load_course_hint(ai_budget, arguments.data / "packages")
        course_diagnose = load_course_diagnose(ai_budget, arguments.data / "packages")
        course_follow = load_course_follow(ai_budget, arguments.data / "packages")
        ai_access = AiAccess.from_setting(os.environ.get("PICOPG_AI_PRINCIPALS"))
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
        course_ai=course_ai,
        course_agent=course_agent,
        ai_access=ai_access,
        ai_budget=ai_budget,
        course_hint=course_hint,
        course_diagnose=course_diagnose,
        course_follow=course_follow,
    )
    host, port = server.server_address
    logger.info(
        "replica server listening on http://%s:%s with %d Log binding(s)",
        host, port, len(server.bindings),
    )
    if course_ai is None:
        logger.info("course AI disabled; clients will use their offline answers")
    elif not ai_access.everyone and not ai_access.principals:
        logger.info("course AI configured but PICOPG_AI_PRINCIPALS is empty; nobody's code is sent to the model")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

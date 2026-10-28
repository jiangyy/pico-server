from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from picopg.server.auth.http import dispatch_session
from picopg.server.ai import CourseAgent, CourseAi
from picopg.server.ai.http import dispatch_course_ai
from picopg.server.auth.model import (
    CompositeAuthenticator,
    CompositeReplicaDirectory,
    PrincipalAccount,
    PrincipalLogAuthorization,
    PrincipalSession,
    StaticAuthenticator,
    StaticReplicaDirectory,
)
from picopg.server.review.comments import CommentPublisher
from picopg.server.playgrounds.releases import (
    PLAYGROUND_RELEASES_LOG_ID,
    PlaygroundLogRule,
    PlaygroundPublisher,
)
from picopg.server.playgrounds.packages import FilePackageStore, PackageAdapter
from picopg.server.playgrounds.http import dispatch_playground
from picopg.server.replication import (
    LogBinding,
    ReplicaHttpServer,
    ReplicaRequestHandler,
)
from picopg.server.auth.registry import FileSessionRegistry
from picopg.server.classroom.http import dispatch_classroom
from picopg.server.classroom.roster import ClassRosterRepository
from picopg.server.review.http import dispatch_review
from picopg.server.submissions.reader import SubmissionReader
from picopg.server.submissions.http import dispatch_student
from picopg.server.drafts.protocol import DraftLogRule
from picopg.server.drafts.worker import DraftRequestConsumer, draft_request_logs, is_draft_request_log
from picopg.server.submissions.logs import (
    SubmissionLogRule,
    is_request_log,
    request_logs as submission_request_logs,
)
from picopg.server.submissions.worker import SubmissionRequestConsumer, SubmissionTagWriter
from picopg.server.analysis import LearningAnalysisLogRule
from picopg.server.review.teacher import dispatch_teacher
from picopg.server.common.workers import ServerWorkerThread
from picopg.replica import ReplicaLogStore
from picopg.replica.file_store import FileReplicaLogStore


class PicoPGHttpServer(ReplicaHttpServer):
    def __init__(
        self,
        address: tuple[str, int],
        store: ReplicaLogStore,
        token: str,
        package_root: Path,
        bindings: Iterable[LogBinding] | None = None,
        package_adapter: PackageAdapter | None = None,
        principal_sessions: Iterable[PrincipalSession] = (),
        principal_accounts: Iterable[PrincipalAccount] = (),
        session_registry: FileSessionRegistry | None = None,
        secure_cookies: bool = True,
        submission_tag_writer: SubmissionTagWriter | None = None,
        course_ai: CourseAi | None = None,
        course_agent: CourseAgent | None = None,
    ) -> None:
        self.workers = None
        sessions = tuple(principal_sessions)
        accounts = tuple(principal_accounts)
        if accounts and session_registry is None:
            raise ValueError("principal accounts require a Session registry")
        if any(session.grants.get(PLAYGROUND_RELEASES_LOG_ID) == "append" for session in sessions):
            raise ValueError(f"{PLAYGROUND_RELEASES_LOG_ID!r} is server-owned and read-only")
        exposed = list(bindings or ())
        release_access = next(
            (access for log_id, access in exposed if log_id == PLAYGROUND_RELEASES_LOG_ID),
            None,
        )
        if release_access is None:
            exposed.append((PLAYGROUND_RELEASES_LOG_ID, "read"))
        elif release_access != "read":
            raise ValueError(
                f"{PLAYGROUND_RELEASES_LOG_ID!r} is server-owned and must be read-only"
            )
        authenticators = ([StaticAuthenticator(sessions)] if sessions else []) + (
            [session_registry] if session_registry is not None else []
        )
        directories = ([StaticReplicaDirectory(sessions)] if sessions else []) + (
            [session_registry] if session_registry is not None else []
        )
        authenticator = CompositeAuthenticator(authenticators) if authenticators else None
        directory = CompositeReplicaDirectory(directories)
        rules = (
            PlaygroundLogRule(),
            DraftLogRule(),
            SubmissionLogRule(),
            LearningAnalysisLogRule(),
        )
        authorization = (
            PrincipalLogAuthorization(
                sessions,
                directory,
                accounts,
                rules,
            )
            if authenticators
            else None
        )
        super().__init__(
            address,
            store,
            token,
            bindings=exposed,
            handler_class=PicoRequestHandler,
            authenticator=authenticator,
            log_authorization=authorization,
            operator_authenticator=(
                session_registry.authenticate_operator if session_registry is not None else None
            ),
        )
        self.principal_accounts = accounts
        self.session_registry = session_registry
        self.secure_cookies = secure_cookies
        self.course_ai = course_ai
        self.course_agent = course_agent
        self.playgrounds = PlaygroundPublisher(
            self, FilePackageStore(package_root), package_adapter
        )
        self.comments = CommentPublisher(self)
        self.submissions = SubmissionReader(self.store)
        self.class_rosters = ClassRosterRepository(
            self,
            (principal.principal_id for principal in (*sessions, *accounts)),
        )
        worker_bindings = dict(self.bindings)
        request_logs = tuple(
            sorted(
                set(draft_request_logs(worker_bindings))
                | {log_id for log_id in self.store.log_ids() if is_draft_request_log(log_id)}
            )
        )
        self.draft_consumer = DraftRequestConsumer(
            self.store,
            request_logs,
            lambda log_id, head: self.broadcast(log_id, head),
        )
        consumers = [self.draft_consumer]
        self.submission_consumer = None
        if submission_tag_writer is not None:
            submission_logs = tuple(
                sorted(
                    set(submission_request_logs(worker_bindings))
                    | {log_id for log_id in self.store.log_ids() if is_request_log(log_id)}
                )
            )
            self.submission_consumer = SubmissionRequestConsumer(
                self.store,
                submission_logs,
                submission_tag_writer,
                lambda log_id, head: self.broadcast(log_id, head),
            )
            consumers.append(self.submission_consumer)
        self.workers = ServerWorkerThread(tuple(consumers)) if consumers else None
        if self.workers is not None:
            self.workers.start()

    def append_and_broadcast(self, log_id, expected_head, records):
        result = super().append_and_broadcast(log_id, expected_head, records)
        if result.kind == "accepted" and self.workers is not None:
            if is_draft_request_log(log_id):
                self.draft_consumer.add_request_log(log_id)
            if self.submission_consumer is not None and is_request_log(log_id):
                self.submission_consumer.add_request_log(log_id)
            self.workers.notify_append(log_id)
        return result

    def server_close(self) -> None:
        if self.workers is not None:
            self.workers.close()
        super().server_close()


class PicoRequestHandler(ReplicaRequestHandler):
    server: PicoPGHttpServer

    def _dispatch(self) -> None:
        path = urlsplit(self.path).path
        if dispatch_session(self, path):
            return
        if dispatch_course_ai(self, path):
            return
        if dispatch_student(self, path):
            return
        if dispatch_classroom(self, path):
            return
        if dispatch_teacher(self, path):
            return
        if dispatch_review(self, path):
            return
        if dispatch_playground(self, path):
            return
        super()._dispatch()


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
    course_ai: CourseAi | None = None,
    course_agent: CourseAgent | None = None,
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
        course_ai=course_ai,
        course_agent=course_agent,
    )

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from apps.server.authentication.http import dispatch_session
from apps.server.authentication.model import (
    CompositeAuthenticator,
    CompositeReplicaDirectory,
    PrincipalAccount,
    PrincipalLogAuthorization,
    PrincipalSession,
    StaticAuthenticator,
    StaticReplicaDirectory,
)
from apps.server.review.comments import CommentPublisher
from apps.server.playground.catalog import (
    PLAYGROUND_RELEASES_LOG_ID,
    PlaygroundLogRule,
    PlaygroundPublisher,
)
from apps.server.playground.package import FilePackageStore, PackageAdapter
from apps.server.playground.http import dispatch_playground
from apps.server.protocol import (
    LogBinding,
    ReplicaHttpServer,
    ReplicaRequestHandler,
)
from apps.server.authentication.registry import FileSessionRegistry
from apps.server.classroom.http import dispatch_classroom
from apps.server.classroom.roster import ClassRosterRepository
from apps.server.review.http import dispatch_review
from apps.server.submission.read_model import SubmissionReader
from apps.server.student.http import dispatch_student
from apps.server.draft.protocol import DraftLogRule
from apps.server.draft.worker import DraftRequestConsumer, draft_request_logs, is_draft_request_log
from apps.server.submission.logs import (
    SubmissionLogRule,
    is_request_log,
    request_logs as submission_request_logs,
)
from apps.server.submission.worker import SubmissionRequestConsumer, SubmissionTagWriter
from apps.server.teacher.http import dispatch_teacher
from apps.server.worker_thread import ServerWorkerThread
from picopg.api import ReplicaLogStore


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
            (
                access
                for log_id, access in exposed
                if log_id == PLAYGROUND_RELEASES_LOG_ID
            ),
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
        rules = (PlaygroundLogRule(), DraftLogRule(), SubmissionLogRule())
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
                session_registry.authenticate_operator
                if session_registry is not None else None
            ),
        )
        self.principal_accounts = accounts
        self.session_registry = session_registry
        self.secure_cookies = secure_cookies
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
        request_logs = tuple(sorted(set(draft_request_logs(worker_bindings)) | {
            log_id for log_id in self.store.log_ids() if is_draft_request_log(log_id)
        }))
        self.draft_consumer = DraftRequestConsumer(
            self.store,
            request_logs,
            lambda log_id, head: self.broadcast(log_id, head),
        )
        consumers = [self.draft_consumer]
        self.submission_consumer = None
        if submission_tag_writer is not None:
            submission_logs = tuple(sorted(set(submission_request_logs(worker_bindings)) | {
                log_id for log_id in self.store.log_ids() if is_request_log(log_id)
            }))
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

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import unquote

from picopg.server.ai.model import CourseAiError
from picopg.server.classroom.roster import ClassRosterError
from picopg.server.learner import for_course
from picopg.server.replication import ResponseSent, logger
from picopg.server.review.comments import CommentError
from picopg.server.submissions.views import detail_value, summary_value
from picopg.server.submissions.reader import SubmissionError, SubmissionNotFound
from picopg.replica import InvalidReplicaData

if TYPE_CHECKING:
    from picopg.server.app import PicoRequestHandler


def dispatch_teacher(handler: PicoRequestHandler, path: str) -> bool:
    route = _teacher_route(path)
    if route is None:
        return False
    try:
        identity = handler.authenticated_replica()
        if identity is None:
            handler.error(401, "auth.required", "valid Session required")
            return True
        action, request_record_id = route
        directory = handler.server.class_rosters.directory()
        students = directory.students_of(identity.principal_id)
        if action == "classes" and handler.command == "GET":
            # 每个班、每个学生的画像；某个学生的画像算不出来时只缺他一个。
            handler.send_json(200, {"classes": [
                {"classId": roster.class_id, "students": [
                    {"principalId": student, "profile": _profile(handler, student)} for student in sorted(roster.students)
                ]}
                for roster in directory.classes_of(identity.principal_id)
            ]}, (("Cache-Control", "no-store"),))
            return True
        if action in ("classes", "activity") and handler.command != "GET":
            handler.error(405, "method.not_allowed", "method not allowed")
            return True
        if action == "activity":
            if request_record_id not in students:
                handler.error(404, "student.not_found", "student is not in your classes")
                return True
            handler.send_json(200, {"activity": handler.server.learners.activity(request_record_id)}, (("Cache-Control", "no-store"),))
            return True
        if action == "list" and handler.command == "GET":
            submissions = handler.server.submissions.list_for_principals(students)
            handler.send_json(200, {
                "submissions": [summary_value(item) for item in submissions]
            }, (("Cache-Control", "no-store"),))
            return True
        submission = handler.server.submissions.get_for(request_record_id, students)
        if action == "detail" and handler.command == "GET":
            handler.send_json(
                200,
                detail_value(handler.server.store, handler.server.comments, submission),
                (("Cache-Control", "no-store"),),
            )
            return True
        if action == "draft" and handler.command == "POST":
            # 和学生用 AI 同一道门：名单与预算都按老师算。
            if handler.server.course_agent is None or not handler.server.ai_access.allows(identity.principal_id):
                handler.error(403, "ai.not_permitted", "AI is not enabled for this teacher")
                return True
            if not handler.server.ai_budget.admit(identity.principal_id):
                handler.error(429, "ai.limited", "AI request limit reached; try again later")
                return True
            source = detail_value(handler.server.store, handler.server.comments, submission)["source"]
            release = next((item for item in handler.server.playgrounds.catalog() if item.playground_id == submission.playground_id), None)
            learner = for_course(
                handler.server.learners.profile(submission.principal_id), submission.playground_id,
                handler.server.lessons.knowledge(), datetime.now(timezone.utc),
            )
            text = handler.server.course_agent.draft_comment(
                submission.playground_id, "" if release is None else release.version_id, str(source), learner,
            )
            handler.send_json(200, {"text": text}, (("Cache-Control", "no-store"),))
            return True
        if action == "comment" and handler.command == "POST":
            body = handler.read_json()
            if not isinstance(body, dict) or set(body) != {"text"}:
                raise CommentError("comment body must contain text")
            position = handler.server.comments.add_submission(
                submission.principal_id,
                submission.playground_id,
                identity.principal_id,
                body["text"],
                submission.submission_id,
                submission.request,
                submission.revision,
            )
            handler.send_json(201, {"position": position.to_value()})
            return True
        handler.error(405, "method.not_allowed", "method not allowed")
    except SubmissionNotFound as error:
        handler.error(404, "submission.not_found", str(error))
    except CourseAiError as error:
        handler.error(502, "ai.reply.invalid", str(error))
    except (ClassRosterError, CommentError, SubmissionError, InvalidReplicaData) as error:
        handler.error(400, "submission.invalid", str(error))
    except ResponseSent:
        pass
    except Exception:
        logger.exception("teacher request failed")
        handler.error(500, "server.failure", "server failure")
    return True


def _profile(handler: PicoRequestHandler, student: str) -> dict[str, object] | None:
    try:
        return handler.server.learners.profile(student)
    except Exception:
        logger.exception("learner profile failed")
        return None


def _teacher_route(path: str) -> tuple[str, str] | None:
    if path == "/api/v1/teacher/submissions":
        return "list", ""
    if path == "/api/v1/teacher/classes":
        return "classes", ""
    parts = path.split("/")
    if len(parts) == 7 and parts[:5] == ["", "api", "v1", "teacher", "students"] and parts[6] == "activity" and parts[5]:
        return "activity", unquote(parts[5])
    if parts[:5] != ["", "api", "v1", "teacher", "submissions"]:
        return None
    if len(parts) == 6:
        request_record_id = unquote(parts[5])
        return None if not request_record_id else ("detail", request_record_id)
    if len(parts) == 7 and parts[6] in ("comments", "draft"):
        request_record_id = unquote(parts[5])
        return None if not request_record_id else ("comment" if parts[6] == "comments" else "draft", request_record_id)
    return None

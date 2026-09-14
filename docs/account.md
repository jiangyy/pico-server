# Account and authorization contract

## Roles

Each active user has one role:

- `Student`: installs content, owns Work, and creates Submissions.
- `Teacher`: manages Releases for assigned students and reviews their
  Submissions.
- `Admin`: manages accounts, relationships, and Playground catalog policy.

Admin does not automatically gain access to student source. Exceptional support
access requires a separate, audited policy if it is ever needed.

## Account lifecycle

- Public registration creates Students only.
- Teacher and Admin accounts are created or promoted by Admin.
- Disabled users cannot create sessions; existing sessions are revoked.
- Password changes revoke other sessions.
- Passwords use a memory-hard password hash with per-password salt.
- Login responses do not reveal whether an account exists.

## Sessions

Session tokens are random opaque secrets. Server stores a hash plus user,
creation, expiry, and revocation timestamps. Logout revokes the current session.
Rate-limit registration, login, and password endpoints.

## Relationships

`TeacherStudent(teacherId, studentId)` is unique and explicitly managed by
Admin. It controls:

- which open Releases a Student receives;
- which Students appear in a Teacher's roster;
- which Submissions a Teacher may review.

In v0.1 Release audience follows current relationships. Removing a relationship
removes future visibility and review access; it does not delete historical rows.

## Authorization matrix

| Action | Student | Teacher | Admin |
| --- | --- | --- | --- |
| Read public content | yes | yes | yes |
| Read released content | own open Releases | own Releases | policy only |
| Read/write live Work | own only | no | no |
| Create Release | no | yes | no |
| Create Submission | own only | no | no |
| Read Submission | own | own Releases | no by default |
| Add Comment | no | own Releases | no |
| Manage users/relationships/catalog | no | no | yes |

Trusted Server jobs may add `ai` or `system` Comments; this is not a user role.

Every check is performed by Server from the authenticated actor and current
database facts. Client UI visibility is convenience, not authorization.

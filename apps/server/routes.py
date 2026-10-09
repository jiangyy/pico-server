"""URL shapes: which path a request names, and which methods it accepts.

Every route in this service is a string pattern over one path, and each one is
matched twice per request — once to route it, once by `_allowed_methods` to
answer `405` with an `Allow` header. Keeping the patterns in one module means
there is a single place to read the whole API, a single place to add the next
endpoint, and something that can be tested without a running server.

The matchers are pure: a path in, the parsed identifiers out, or ``None`` when
the path is not that shape. They never touch the database, the repository or the
request body.
"""

# --- Paths ------------------------------------------------------------------

PLAYGROUNDS_PATH = "/api/v1/playgrounds"
PLAYGROUND_VERSIONS_SUFFIX = "/versions"
PLAYGROUND_RESTORE_SUFFIX = "/restore"
CATALOG_PATH = "/api/v1/catalog"
ASSIGNMENTS_PATH = "/api/v1/assignments"

#: The literal segments a read route is built from.
VERSIONS_SEGMENT = "versions"
FILES_SEGMENT = "files"
MANIFEST_SEGMENT = "manifest"

#: What a plain read accepts.
ALLOWED_METHODS = ("GET", "HEAD")


def _match_playground_versions(path):
    """Return the playground id in a version-upload path, else ``None``.

    ``POST /api/v1/playgrounds/<id>/versions`` — the publisher's one write.
    """

    prefix = PLAYGROUNDS_PATH + "/"
    if not path.startswith(prefix) or not path.endswith(
        PLAYGROUND_VERSIONS_SUFFIX
    ):
        return None

    playground_id = path[len(prefix): -len(PLAYGROUND_VERSIONS_SUFFIX)]
    if not playground_id or "/" in playground_id:
        return None
    return playground_id


def match_playground_restore(path):
    """Return the playground id in a restore path, else ``None``.

    ``POST /api/v1/playgrounds/<id>/restore`` — the counterpart of the
    ``DELETE`` on the same path.
    """

    prefix = PLAYGROUNDS_PATH + "/"
    if not path.startswith(prefix) or not path.endswith(
        PLAYGROUND_RESTORE_SUFFIX
    ):
        return None

    playground_id = path[len(prefix): -len(PLAYGROUND_RESTORE_SUFFIX)]
    if not playground_id or "/" in playground_id:
        return None
    return playground_id


def match_playground(path):
    """Return the playground id in ``/api/v1/playgrounds/<id>``, else ``None``.

    ``DELETE`` is the only thing this shape answers.
    """

    prefix = PLAYGROUNDS_PATH + "/"
    if not path.startswith(prefix):
        return None

    playground_id = path[len(prefix):]
    if not playground_id or "/" in playground_id:
        return None
    return playground_id


def match_assignment_versions(path):
    """Return the assignment id in ``/api/v1/assignments/<id>/versions``."""

    prefix = ASSIGNMENTS_PATH + "/"
    if not path.startswith(prefix) or not path.endswith(PLAYGROUND_VERSIONS_SUFFIX):
        return None
    assignment_id = path[len(prefix): -len(PLAYGROUND_VERSIONS_SUFFIX)]
    if not assignment_id or "/" in assignment_id:
        return None
    return assignment_id


def match_assignment(path):
    """Return the assignment id in ``/api/v1/assignments/<id>``, else ``None``."""

    prefix = ASSIGNMENTS_PATH + "/"
    if not path.startswith(prefix):
        return None
    assignment_id = path[len(prefix):]
    if not assignment_id or "/" in assignment_id:
        return None
    return assignment_id


def assignment_versions_endpoint(assignment_id):
    """The idempotency endpoint of an Assignment upload."""

    return f"POST {ASSIGNMENTS_PATH}/{assignment_id}{PLAYGROUND_VERSIONS_SUFFIX}"


def playground_versions_endpoint(playground_id):
    """The idempotency endpoint of a Playground's bundle upload.

    One spelling, used by both the upload that records a result and the archive
    that has to forget it, so the two can never drift apart.
    """

    return f"POST {PLAYGROUNDS_PATH}/{playground_id}{PLAYGROUND_VERSIONS_SUFFIX}"


def _version_route_parts(path):
    """Split ``.../<playground>/versions/<version>/<tail>`` for read routes.

    Returns ``(playground_id, version_id, tail)`` where ``tail`` is everything
    after ``<version>/`` and may still contain literal slashes. ``None`` for any
    other shape, so a missing or extra segment never matches.

    ``version_id`` is a Git commit id in this implementation; the protocol only
    promises it addresses a Version.
    """

    prefix = PLAYGROUNDS_PATH + "/"
    if not path.startswith(prefix):
        return None

    parts = path[len(prefix):].split("/", 3)
    if len(parts) != 4:
        return None
    playground_id, segment, version_id, tail = parts
    if segment != VERSIONS_SEGMENT or not playground_id or not version_id:
        return None
    return playground_id, version_id, tail


def match_manifest(path):
    """Return ``(playground_id, version_id)`` for a manifest read path."""

    parts = _version_route_parts(path)
    if parts is None:
        return None
    playground_id, version_id, tail = parts
    if tail != MANIFEST_SEGMENT:
        return None
    return playground_id, version_id


def match_version_files(path):
    """Return ``(playground_id, version_id, encoded_path)`` for a file path.

    The path is still percent-encoded here: decoding is the caller's job, so a
    route match never has to think about it.
    """

    parts = _version_route_parts(path)
    if parts is None:
        return None
    playground_id, version_id, tail = parts
    prefix = FILES_SEGMENT + "/"
    if not tail.startswith(prefix):
        return None
    encoded_path = tail[len(prefix):]
    if not encoded_path:
        return None
    return playground_id, version_id, encoded_path


def allowed_methods(path):
    """Return the methods a known path accepts, or ``None`` when unknown.

    This is what turns "known path, wrong verb" into a `405` carrying `Allow`
    instead of a `404`, so a caller can tell a typo'd path from a typo'd method.
    """

    if path in ("/healthz", CATALOG_PATH):
        return ALLOWED_METHODS
    if path == ASSIGNMENTS_PATH:
        return ("POST",)
    if match_assignment(path) is not None:
        return ("DELETE",)
    if match_assignment_versions(path) is not None:
        return ("POST",)
    if path == PLAYGROUNDS_PATH:
        return ("POST",)
    if match_playground(path) is not None:
        return ("DELETE",)
    if _match_playground_versions(path) is not None:
        return ("POST",)
    if match_playground_restore(path) is not None:
        return ("POST",)
    if match_manifest(path) is not None:
        return ALLOWED_METHODS
    if match_version_files(path) is not None:
        return ALLOWED_METHODS
    return None


#: The one internal spelling the router uses, so `server.py` does not have to
#: know that the upload route is the only private matcher.
match_playground_versions = _match_playground_versions

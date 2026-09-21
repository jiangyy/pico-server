"""The route table, tested as the pure function it is.

Every route used to be reachable only by starting a server and issuing an HTTP
request, so a shape that matched *too much* — an id containing a slash, an extra
segment, a prefix that is also a prefix of another route — could only be noticed
by someone thinking to try it. Here each matcher is asked directly.
"""

import unittest

import routes


class PlaygroundMatchersTest(unittest.TestCase):
    def test_the_upload_path_is_the_only_thing_that_matches_it(self):
        self.assertEqual(
            routes.match_playground_versions("/api/v1/playgrounds/pg/versions"),
            "pg",
        )
        for path in (
            "/api/v1/playgrounds/pg",
            "/api/v1/playgrounds/pg/versions/extra",
            "/api/v1/playgrounds/pg/versionsx",
            "/api/v1/playgrounds//versions",
            "/api/v1/playgrounds/a/b/versions",
            "/api/v1/playgrounds",
        ):
            self.assertIsNone(routes.match_playground_versions(path), path)

    def test_the_playground_path_does_not_swallow_a_sub_path(self):
        self.assertEqual(routes.match_playground("/api/v1/playgrounds/pg"), "pg")
        for path in (
            "/api/v1/playgrounds",
            "/api/v1/playgrounds/",
            "/api/v1/playgrounds/pg/versions",
            "/api/v1/playgrounds/pg/anything",
            "/api/v1/playground",
        ):
            self.assertIsNone(routes.match_playground(path), path)

    def test_the_idempotency_endpoint_has_one_spelling(self):
        self.assertEqual(
            routes.playground_versions_endpoint("pg"),
            "POST /api/v1/playgrounds/pg/versions",
        )


class VersionRouteMatchersTest(unittest.TestCase):
    VERSION = "a" * 40

    def test_the_manifest_shape(self):
        self.assertEqual(
            routes.match_manifest(f"/api/v1/playgrounds/pg/versions/{self.VERSION}/manifest"),
            ("pg", self.VERSION),
        )
        for path in (
            f"/api/v1/playgrounds/pg/versions/{self.VERSION}",
            f"/api/v1/playgrounds/pg/versions/{self.VERSION}/manifest/extra",
            f"/api/v1/playgrounds/pg/commits/{self.VERSION}/manifest",
            f"/api/v1/playgrounds/pg/versions//manifest",
            "/api/v1/playgrounds/pg/versions/manifest",
        ):
            self.assertIsNone(routes.match_manifest(path), path)

    def test_a_file_path_keeps_its_encoded_tail(self):
        self.assertEqual(
            routes.match_version_files(f"/api/v1/playgrounds/pg/versions/{self.VERSION}/files/course/a.py"),
            ("pg", self.VERSION, "course/a.py"),
        )
        # Nested and encoded paths stay one string: decoding is the caller's job.
        self.assertEqual(
            routes.match_version_files(f"/api/v1/playgrounds/pg/versions/{self.VERSION}/files/a/b/c.css"),
            ("pg", self.VERSION, "a/b/c.css"),
        )
        self.assertEqual(
            routes.match_version_files(f"/api/v1/playgrounds/pg/versions/{self.VERSION}/files/a%2Fb"),
            ("pg", self.VERSION, "a%2Fb"),
        )
        for path in (
            f"/api/v1/playgrounds/pg/versions/{self.VERSION}/files",
            f"/api/v1/playgrounds/pg/versions/{self.VERSION}/files/",
            f"/api/v1/playgrounds/pg/versions/{self.VERSION}/manifest",
        ):
            self.assertIsNone(routes.match_version_files(path), path)


class SubmissionMatcherTest(unittest.TestCase):
    def test_only_a_single_opaque_segment_matches(self):
        self.assertEqual(routes.match_submission("/api/v1/submissions/abc"), "abc")
        for path in (
            "/api/v1/submissions",
            "/api/v1/submissions/",
            "/api/v1/submissions/a/b",
            "/api/v1/submission/abc",
        ):
            self.assertIsNone(routes.match_submission(path), path)


class MethodTableTest(unittest.TestCase):
    VERSION = "a" * 40

    def test_every_known_path_reports_its_methods(self):
        table = {
            "/healthz": ("GET", "HEAD"),
            "/api/v1/catalog": ("GET", "HEAD"),
            "/api/v1/submissions": ("GET", "HEAD"),
            "/api/v1/submissions/abc": ("GET", "HEAD"),
            "/api/v1/sync": ("POST",),
            "/api/v1/playgrounds": ("POST",),
            "/api/v1/playgrounds/pg": ("DELETE",),
            "/api/v1/playgrounds/pg/versions": ("POST",),
            f"/api/v1/playgrounds/pg/versions/{self.VERSION}/manifest": ("GET", "HEAD"),
            f"/api/v1/playgrounds/pg/versions/{self.VERSION}/files/a.py": ("GET", "HEAD"),
        }
        for path, methods in table.items():
            self.assertEqual(routes.allowed_methods(path), methods, path)

    def test_an_unknown_path_has_no_methods(self):
        for path in ("/", "/api/v1", "/api/v1/nope", "/api/v1/catalogx", "/favicon.ico"):
            self.assertIsNone(routes.allowed_methods(path), path)

    def test_a_known_path_with_a_wrong_verb_is_told_so(self):
        # The table's whole job: a caller learns the path exists but not like
        # this, instead of being told the path does not exist.
        self.assertIsNotNone(routes.allowed_methods("/api/v1/catalog"))
        self.assertIsNotNone(routes.allowed_methods("/api/v1/sync"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

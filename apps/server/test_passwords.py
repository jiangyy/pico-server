"""Tests for password hashing primitives."""

import unittest

from passwords import hash_password, verify_password


class PasswordTest(unittest.TestCase):
    def test_password_round_trip(self):
        encoded = hash_password("correct horse battery staple")

        self.assertTrue(verify_password("correct horse battery staple", encoded))
        self.assertFalse(verify_password("wrong password", encoded))

    def test_hashes_use_independent_salts(self):
        first = hash_password("same password")
        second = hash_password("same password")

        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("same password", first))
        self.assertTrue(verify_password("same password", second))

    def test_unicode_password_round_trip(self):
        encoded = hash_password("学习 Python 🐍")

        self.assertTrue(verify_password("学习 Python 🐍", encoded))

    def test_malformed_or_unsupported_hash_is_rejected(self):
        encoded = hash_password("password")
        cases = (
            "",
            "not-a-password-hash",
            encoded.replace("scrypt-v1", "scrypt-v2", 1),
            encoded.replace("$16384$", "$32768$", 1),
            encoded.rsplit("$", 1)[0] + "$invalid base64",
        )

        for value in cases:
            with self.subTest(value=value):
                self.assertFalse(verify_password("password", value))


if __name__ == "__main__":
    unittest.main(verbosity=2)

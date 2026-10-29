from __future__ import annotations

import unittest

from picopg.server.ai.access import AiAccess


class AiAccessTest(unittest.TestCase):
    def test_nobody_unless_listed(self) -> None:
        assert not AiAccess.from_setting(None).allows("student")
        assert not AiAccess.from_setting("").allows("student")
        listed = AiAccess.from_setting(" student , teacher ")
        assert listed.allows("student") and listed.allows("teacher") and not listed.allows("other")
        assert AiAccess.from_setting("*").allows("anyone")
        with self.assertRaises(ValueError):
            AiAccess.from_setting("*,student")

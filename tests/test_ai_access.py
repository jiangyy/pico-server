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


class AiBudgetTest(unittest.TestCase):
    def test_hourly_requests_per_principal_and_daily_tokens(self) -> None:
        from picopg.server.ai.budget import AiBudget

        now = [0.0]
        budget = AiBudget(per_hour=2, daily_tokens=100, clock=lambda: now[0])
        assert budget.admit("a") and budget.admit("a") and not budget.admit("a")
        assert budget.admit("b"), "one principal's use does not limit another"
        now[0] = 3600
        assert budget.admit("a"), "the hourly window slides"
        budget.spend(100)
        assert not budget.admit("c"), "the daily token cap stops everyone"
        now[0] = 86_400
        assert budget.admit("c"), "the cap resets the next UTC day"

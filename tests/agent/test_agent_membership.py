"""Synthetic bounded membership observations from the family record's clan stays."""

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.datasets.membership import read_membership_history
from elbow_helper.agent.models import AgentTurnState

HOUR = 3600 * 1_000_000


def at(day, hour=0):
    return int(datetime(2026, 10, day, hour, tzinfo=timezone.utc).timestamp() * 1_000_000)


def stay(code, from_at, last, to_at=None, basis="record"):
    return {"clan_tag": CLANS[code].tag, "clan_code": code, "from_at": from_at, "to_at": to_at,
            "first_seen_at": from_at, "last_seen_at": last, "basis": basis, "player_name": "Synthetic"}


class MembershipReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.codes = list(CLANS)[:2]
        self.rows = []
        self.reads = SimpleNamespace(movements=AsyncMock(side_effect=lambda **window: {
            **window, "rows": self.rows, "truncated": False}))
        self.context = SimpleNamespace(bot=SimpleNamespace(stay_reads=self.reads), state=AgentTurnState())
        patcher = patch("elbow_helper.agent.datasets.membership.require_evidence_access", AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.patch_record = patch("elbow_helper.agent.record_identity.require_evidence_access", AsyncMock())
        self.patch_record.start()
        self.addCleanup(self.patch_record.stop)
        self.arguments = {"after": "2026-10-02", "before": "2026-10-07", "history_after": "2026-10-01"}

    def account(self, tag, *stays):
        self.rows.append({"player_tag": tag, "stays": list(stays)})

    async def test_new_returning_and_transfer_observations_include_bounds(self):
        first, second = self.codes
        self.account("#P0", stay(first, at(1), at(2, 23), at(3)), stay(second, at(3), at(6)))
        self.account("#P2", stay(first, at(1), at(1, 12), at(1, 13)), stay(first, at(4), at(6)))
        self.account("#P8", stay(first, at(3), at(6)))
        result = await read_membership_history(self.context, self.arguments)
        window = self.reads.movements.await_args.kwargs
        self.assertEqual((window["after"], window["before"]), (at(1), at(7)))
        events = {row["player_tag"]: row for row in result["events"]}
        self.assertEqual((events["#P0"]["kind"], events["#P0"]["previous_clan_code"]), ("family_transfer", first))
        self.assertEqual(events["#P2"]["kind"], "returning_observation")
        self.assertEqual(events["#P2"]["prior_membership_observed_at"], "2026-10-01T00:00:00+00:00")
        self.assertTrue(events["#P2"]["sighting_gap"])
        self.assertEqual(events["#P8"]["kind"], "first_observed")
        self.assertTrue(all(not row["first_ever_membership_known"] for row in events.values()))
        self.assertEqual(result["coverage"]["observed_after"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(result["source"], "family_clan_stays")
        page = await read_membership_history(self.context, {**self.arguments, "limit": 1})
        self.assertEqual(page["next_offset"], 1)
        next_page = await read_membership_history(self.context, {**self.arguments, "offset": 1, "limit": 1})
        self.assertNotEqual(page["events"], next_page["events"])
        only = await read_membership_history(self.context, {**self.arguments, "clan_codes": [second]})
        self.assertEqual([row["player_tag"] for row in only["events"]], ["#P0"])

    async def test_a_copied_stay_carried_into_the_record_is_not_an_arrival(self):
        code = self.codes[0]
        self.account("#P0", stay(code, at(1), at(3), at(3), basis="observed_hourly"), stay(code, at(3), at(6)))
        # An hourly bounce back into the same clan continues the stay too.
        self.account("#P2", stay(code, at(1), at(4), basis="observed_hourly"),
                     stay(code, at(4, 1), at(6), basis="observed_hourly"))
        result = await read_membership_history(self.context, self.arguments)
        self.assertEqual(result["events"], [])

    async def test_sightings_before_the_history_bound_do_not_make_a_return(self):
        code = self.codes[0]
        self.account("#P0", stay(code, at(1) - 10 * 24 * HOUR, at(1) + HOUR), stay(code, at(4), at(6)))
        result = await read_membership_history(self.context, {**self.arguments})
        event = result["events"][0]
        self.assertEqual(event["kind"], "returning_observation")
        # The earlier stay counts only from the history bound.
        self.assertEqual(event["prior_membership_observed_at"], "2026-10-01T00:00:00+00:00")

    async def test_refused_windows_unreadable_and_oversized_records_are_explicit(self):
        result = await read_membership_history(self.context, {**self.arguments, "history_after": "2025-01-01"})
        self.assertEqual(result["cause"], "invalid_arguments")
        for extra in ({"limit": 26}, {"before": "bad"}, {"clan_codes": ["NOPE"]}):
            self.assertIn("error", await read_membership_history(self.context, {**self.arguments, **extra}))
        empty = await read_membership_history(self.context, self.arguments)
        self.assertEqual((empty["events"], empty["coverage"]["observed_after"]), ([], None))
        self.reads.movements = AsyncMock(side_effect=OSError("synthetic outage"))
        self.assertEqual((await read_membership_history(self.context, self.arguments))["cause"], "source_unavailable")
        self.reads.movements = AsyncMock(return_value={"rows": [], "truncated": True})
        self.assertEqual((await read_membership_history(self.context, self.arguments))["cause"], "source_unavailable")
        no_record = SimpleNamespace(bot=SimpleNamespace(), state=AgentTurnState())
        self.assertEqual((await read_membership_history(no_record, self.arguments))["cause"], "source_unavailable")


if __name__ == "__main__":
    unittest.main()

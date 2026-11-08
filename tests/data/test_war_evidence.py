"""War source status, type basis, legacy IDs and war coverage on the family record."""

import unittest

from elbow_helper.data.reads.coverage import WAR_CADENCES, envelope, war_side_coverage, war_signal
from elbow_helper.data.store.war_projection import HOUR

from .test_wars import END, SECOND, WarFixture, war, war_log


DAY = 24 * HOUR


class WarCoverageTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def signal(self, kind, scope, at):
        return await self.store.call(war_signal, kind, scope, at)

    async def test_source_freshness_failures_privacy_and_staleness(self):
        self.assertEqual((await self.signal("current_war", "#PY", self.at))["status"], "not_tracked")
        await self.record(war())
        fresh = await self.signal("current_war", "#PY", self.at)
        self.assertEqual((fresh["status"], fresh["last_good_at"], fresh["source_complete"], fresh["observed"]["state"]),
                         ("observed", self.at, True, "inWar"))
        self.assertTrue(envelope([fresh], at=self.at)["complete"])
        await self.record(None, status=503, at=self.at + 300 * SECOND)
        failing = await self.signal("current_war", "#PY", self.at + 300 * SECOND)
        self.assertEqual((failing["status"], failing["last_good_at"], [f["http_status"] for f in failing["failures"]]),
                         ("partial", self.at, [503]))
        await self.record({"reason": "accessDenied"}, status=403, at=self.at + 600 * SECOND)
        self.assertEqual((await self.signal("current_war", "#PY", self.at + 600 * SECOND))["status"], "private")
        await self.record(war(), at=self.at + 900 * SECOND)
        due = (await self.rows("SELECT next_due_at FROM war_collection_state WHERE source_kind='current_war' AND scope_id='#PY'"))[0]["next_due_at"]
        late = due + WAR_CADENCES["current_war"] + SECOND
        stale = await self.signal("current_war", "#PY", late)
        self.assertEqual((stale["status"], stale["source_complete"], stale["gaps"][0]["cause"]), ("stale", False, "overdue"))
        self.assertFalse(envelope([stale], at=late)["complete"])
        # A source that never succeeded is unavailable, not stale.
        await self.record({"reason": "accessDenied"}, kind="war_log", status=403, at=self.at)
        await self.record(None, scope="#LQ", status=503)
        self.assertEqual((await self.signal("war_log", "#PY", self.at))["status"], "private")
        self.assertEqual((await self.signal("current_war", "#LQ", self.at))["status"], "unavailable")

    async def test_log_limits_and_identity_conflicts(self):
        # Two live wars share a clan pair and end, so the log entry stays apart.
        await self.record(war())
        other = war()
        other["preparationStartTime"] = "20261026T170000.000Z"
        await self.record(other)

        async def limits():
            return (await self.signal("war_log", "#PY", self.at))["log_limits"]
        await self.record(war_log(), kind="war_log")
        self.assertEqual(await limits(), {"history_complete": True, "api_history_limit": False, "backfill_pending": False})
        await self.record(war_log(war("warEnded", clans=("#PY", "#CU"))), kind="war_log")
        self.assertEqual(await limits(), {"history_complete": False, "api_history_limit": True, "backfill_pending": False})
        await self.record(war_log(after="next"), kind="war_log")
        self.assertEqual(await limits(), {"history_complete": False, "api_history_limit": False, "backfill_pending": True})
        conflicts = (await self.signal("current_war", "#PY", self.at))["identity_conflicts"]
        self.assertEqual([(c["clan_tags"], c["end_at"], len(c["war_ids"])) for c in conflicts],
                         [(["#GR", "#PY"], END, 3)])
        self.assertEqual((await self.signal("current_war", "#LQ", self.at))["identity_conflicts"], [])

    async def test_own_side_completeness_is_kept_apart_from_the_opponent(self):
        partial = war("warEnded")
        partial["opponent"].pop("members")
        await self.record(partial, at=END + 90 * SECOND)
        war_id = (await self.rows("SELECT id FROM wars"))[0]["id"]
        coverage = await self.store.call(war_side_coverage, war_id, "#PY")
        self.assertEqual((coverage["own"]["clan_tag"], coverage["own"]["lineup_complete"], coverage["own"]["attacks_complete"],
                          coverage["own"]["attacks_stored"]), ("#PY", True, True, 2))
        self.assertEqual((coverage["opponent"]["lineup_complete"], coverage["opponent"]["attacks_complete"],
                          coverage["opponent"]["attacks_stored"], coverage["opponent"]["attacks_reported"]), (False, False, 0, 2))
        # The result is final; the opponent's attack detail is still missing.
        self.assertEqual((coverage["type"], coverage["type_basis"], coverage["final"]), ("REG", "times", True))
        await self.record(war("warEnded"), at=END + 300 * SECOND)
        complete = await self.store.call(war_side_coverage, war_id, "#GR")
        self.assertEqual((complete["own"]["clan_tag"], complete["own"]["attacks_complete"],
                          complete["opponent"]["attacks_complete"], complete["final"]), ("#GR", True, True, True))
        self.assertIsNone(await self.store.call(war_side_coverage, war_id, "#LQ"))
        aggregate = war_log(war("warEnded", clans=("#PY", "#CU")))
        await self.record(aggregate, kind="war_log")
        logged = (await self.rows("SELECT id FROM wars WHERE preparation_at IS NULL"))[0]["id"]
        only_totals = await self.store.call(war_side_coverage, logged, "#PY")
        self.assertEqual((only_totals["own"]["members"], only_totals["own"]["lineup_complete"]), (0, False))

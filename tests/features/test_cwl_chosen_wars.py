"""Chosen CWL wars are checked against synthetic family-record weeks before scoring."""

import asyncio
import json
import tempfile
import unittest

from elbow_helper.configuration.clans import CLANS

from tests.features.cwl_record_stub import SECOND, LeagueRecord, league_war, member

END = 1788000000


class ChosenWarStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.record = await LeagueRecord(temporary.name).initialize()
        await self.record.week("BEH", "2026-08", [
            league_war("#C0", 1, END, [member("#P0")]),
            league_war("#C2", 2, END + 86400, [member("#P0")], state="inWar"),
            # A league war against another family clan belongs to both clans.
            league_war("#C9", 3, END + 2 * 86400, [member("#P2")], opponent=CLANS["BE1"].tag),
        ], league="Champion League II", final=False)
        await self.record.week("BE1", "2026-08", [league_war("#C8", 1, END, [member("#Q0")])],
                               league="Champion League II")
        await self.regular_war("REGULAR")
        self.history = self.record.history()
        self.queries = self.record.queries()

    async def regular_war(self, legacy_id):
        """A regular war of the clan, known by an old clan-health ID."""
        def write(c):
            run = c.execute("SELECT MAX(id) FROM collection_runs").fetchone()[0]
            war_id = c.execute(
                "INSERT INTO wars(type,end_at,state,first_seen_at,last_seen_at,roster_complete,attacks_complete,"
                "result_complete,content_hash,field_presence_json,source_run_id) "
                "VALUES ('REG',?,'warEnded',?,?,1,1,1,'synthetic','[]',?)",
                (END * SECOND, END * SECOND, END * SECOND, run)).lastrowid
            for side, tag in enumerate(sorted((CLANS["BEH"].tag, "#Q8"))):
                c.execute("INSERT INTO war_sides(war_id,side,clan_tag,extra_json,source_run_id) VALUES (?,?,?,?,?)",
                          (war_id, side, tag, json.dumps({}), run))
            c.execute("INSERT INTO war_legacy_ids(source,legacy_id,clan_code,war_id,mapped_at) "
                      "VALUES ('clan_health',?,'BEH',?,0)", (legacy_id, war_id))
            c.commit()
        await self.record.store.call(write)

    async def test_unknown_and_unfinished_wars_are_named_together(self):
        with self.assertRaisesRegex(ValueError, "Unknown CWL war IDs: #UNKNOWN; Unfinished CWL wars: CWL:#C2"):
            await asyncio.to_thread(self.queries.ass_wars, clan_code="BEH", war_ids=["#UNKNOWN", "CWL:#C2"])

    async def test_other_clan_and_regular_wars_are_refused(self):
        for key, error in (("#C8", "another clan than BEH: CWL:#C8"),
                           ("REGULAR", "Not CWL wars: REGULAR")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, error):
                await asyncio.to_thread(self.queries.ass_wars, clan_code="BEH", war_ids=[key])

    async def test_only_the_chosen_finished_war_is_loaded(self):
        wars = await asyncio.to_thread(self.history.scoring_wars, "BEH", ["#C0"])
        self.assertEqual([war["war_id"] for war in wars], ["CWL:#C0"])
        self.assertEqual(wars[0]["state"], "warEnded")
        self.assertEqual([row["player_tag"] for row in wars[0]["roster"]], ["#P0"])
        self.assertEqual(wars[0]["attacks"], [])

    async def test_a_war_between_two_family_clans_uses_the_requested_clan(self):
        wars = await asyncio.to_thread(self.history.scoring_wars, "BEH", ["#C9"])
        self.assertEqual([(war["clan_code"], war["war_id"]) for war in wars], [("BEH", "CWL:#C9")])
        self.assertEqual([row["player_tag"] for row in wars[0]["roster"]], ["#P2"])


if __name__ == "__main__":
    unittest.main()

"""Individual stored war attacks, selected as war attendance selects wars (synthetic).

Wars are written into a family record. No Discord, Clash or LLM call is made.
"""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.reads import record_wars, war_attacks, war_tools as war_reads, wars
from elbow_helper.agent.safety.access import AgentAccessLost
from elbow_helper.data.reads import CwlReads, WarReads
from elbow_helper.features.cwl.queries import CwlQueries
from elbow_helper.features.cwl.record import CwlHistory

from tests.agent.engine.test_agent_plan_flow import _context
from tests.agent.reads.test_family_wars import renamed
from tests.agent.reads.test_record_wars import FAMILY, OCTOBER, later_war, old_id, ts
from tests.data.test_wars import END, SECOND, WarFixture, group, war

DAY = 86400 * SECOND
TOOLS = {tool.definition.name: tool for tool in war_reads.TOOLS}
LEAGUE_END = "20261010T180000.000Z"


class WarAttackTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        for target in ('elbow_helper.infrastructure.clash.ClashClient.get',
                       'elbow_helper.infrastructure.ai.client.DeepSeekTextClient.complete'):
            patcher = patch(target, side_effect=AssertionError('War reads must not call Clash or the LLM'))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.now = END + 5 * DAY
        for patcher in (patch.object(record_wars, "CLANS", FAMILY),
                        patch.object(record_wars, "CODES", {"#PY": "BEH", "#LQ": "BE4"}),
                        patch.object(war_reads, "CLANS", FAMILY),
                        patch.object(wars, "CLANS", FAMILY),
                        patch.object(war_attacks, "CLANS", FAMILY),
                        patch("elbow_helper.data.reads.core.now_us", side_effect=lambda: self.now)):
            patcher.start()
            self.addCleanup(patcher.stop)
        links = SimpleNamespace(get_links_by_tags=lambda tags: {tag: {"discord_user_id": 41} for tag in tags
                                                                 if tag in ("#P0", "#P2")},
                                get_links_for_members=lambda ids: {
                                    41: [{"player_tag": "#P0"}, {"player_tag": "#P2"}]} if 41 in ids else {})
        history = CwlHistory(CwlReads(self.store), None, FAMILY)
        self.context = replace(_context(), cwl_queries=CwlQueries(history, lambda: {}), account_links=links,
                               bot=SimpleNamespace(war_reads=WarReads(self.store)))
        await self.seed()

    async def seed(self):
        # BEH, two regular wars. In the later one #P0 (top base) hits the opponent's second base.
        later = later_war()
        later["clan"]["members"][0]["attacks"][0]["defenderTag"] = "#Q2"
        top, second = later["opponent"]["members"]
        del top["bestOpponentAttack"]
        top["opponentAttacks"], second["opponentAttacks"] = 0, 2
        for payload, moment in ((war("warEnded"), END + 90 * SECOND), (later, END + 2 * DAY + 90 * SECOND)):
            await self.record(payload, at=moment)
        # BE4, one regular war on 20 Oct for #Y0 (a Town Hall 15) and #Y2.
        be4 = renamed(war("warEnded", clans=("#LQ", "#CU"), end="20261020T180000.000Z"), P0="Y0", P2="Y2")
        be4.update(preparationStartTime="20261018T180000.000Z", startTime="20261019T180000.000Z")
        be4["clan"]["members"][0]["townhallLevel"] = 15
        await self.record(be4, scope="#LQ", at=END + 3 * DAY)
        # BEH's league: one CWL war on 10 Oct, where #P0 and #P2 also play.
        league = war("warEnded", clans=("#PY", "#CU"), end=LEAGUE_END)
        league.update(preparationStartTime="20261008T180000.000Z", startTime="20261009T180000.000Z")
        await self.record(group(rounds=[{"warTags": ["#R0", "#R2"]}]), kind="cwl_group",
                          at=ts(league["preparationStartTime"]) * SECOND)
        await self.record(league, kind="cwl_war", scope="#R0", at=ts(LEAGUE_END) * SECOND + 90 * SECOND)

    async def read(self, **arguments):
        return await war_attacks.war_attacks(self.context, {"war_type": "both", **OCTOBER, **arguments})

    @staticmethod
    def brief(result):
        return [(row["war_type"], row["clan_code"], row["player_tag"], row["map_position"],
                 row["defender_map_position"]) for row in result["rows"]]

    async def test_an_accounts_regular_and_cwl_attacks_with_positions(self):
        result = await self.read(account_tags=["#P0"])
        self.assertNotIn("error", result)
        self.assertEqual(self.brief(result), [("REG", "BEH", "#P0", 1, 2), ("REG", "BEH", "#P0", 1, 1),
                                              ("CWL", "BEH", "#P0", 1, 1)])
        latest = result["rows"][0]
        self.assertEqual(latest, {
            "war_id": old_id(later_war()), "war_type": "REG", "war_state": "warEnded",
            "war_end_at": "2026-10-30T18:00:00+00:00", "clan_code": "BEH",
            "opponent_tag": "#CU", "opponent_name": "Clan #CU", "player_tag": "#P0", "player_name": "Player #P0",
            "member_id": 41, "map_position": 1, "townhall": 16, "defender_map_position": 2,
            "defender_townhall": 16, "defender_basis": "lineup", "stars": 3, "destruction": 100.0,
            "attack_order": 1, "duration_seconds": 123})
        self.assertEqual((result["rows"][1]["opponent_tag"], result["rows"][2]["war_id"]), ("#GR", "CWL:#R0"))
        self.assertEqual(result["accounts_without_attacks"], [])
        self.assertEqual((result["coverage"]["wars_checked"], result["coverage"]["wars_not_found"],
                          result["coverage"]["incomplete_wars"]), (3, [], []))
        self.assertIn("map_position", result["meanings"])
        # One war type at a time.
        self.assertEqual([row["war_type"] for row in (await self.read(war_type="CWL", account_tags=["#P0"]))["rows"]],
                         ["CWL"])

    async def test_the_period_and_clan_choose_the_wars(self):
        # A window holding only the later regular war.
        window = await self.read(war_type="REG", after="2026-10-29T00:00:00+00:00",
                                 before="2026-11-01T00:00:00+00:00")
        self.assertEqual({row["war_id"] for row in window["rows"]}, {old_id(later_war())})
        self.assertEqual(len(window["rows"]), 2)
        # A season is that month's regular wars and that league season.
        season = await war_attacks.war_attacks(self.context, {"war_type": "both", "season": "2026-10",
                                                              "clan_codes": ["BEH"]})
        self.assertEqual(len(season["rows"]), 6)
        # One clan: only its accounts, with its own Town Halls.
        be4 = await self.read(war_type="REG", clan_codes=["BE4"])
        self.assertEqual(self.brief(be4), [("REG", "BE4", "#Y0", 1, 1), ("REG", "BE4", "#Y2", 2, 2)])
        self.assertEqual((be4["rows"][0]["townhall"], be4["rows"][0]["member_id"]), (15, None))
        # The latest wars count across the selected clans.
        latest = await war_attacks.war_attacks(self.context, {"war_type": "REG", "latest_completed": 1})
        self.assertEqual({row["war_id"] for row in latest["rows"]}, {old_id(later_war())})
        # A September window has no wars.
        empty = await war_attacks.war_attacks(self.context, {"war_type": "both", "season": "2026-09"})
        self.assertEqual((empty["rows"], empty["coverage"]["wars_checked"]), ([], 0))

    async def test_a_war_between_two_family_clans_lists_each_sides_attacks(self):
        inside = war("warEnded", clans=("#PY", "#LQ"), end="20261025T180000.000Z")
        inside.update(preparationStartTime="20261023T180000.000Z", startTime="20261024T180000.000Z")
        await self.record(inside, at=END + 4 * DAY)
        result = await self.read(war_type="REG", after="2026-10-24T00:00:00+00:00", before="2026-10-26T00:00:00+00:00")
        self.assertEqual(len({row["war_id"] for row in result["rows"]}), 1)
        self.assertEqual(sorted((row["clan_code"], row["player_tag"], row["opponent_tag"]) for row in result["rows"]),
                         [("BE4", "#Q0", "#PY"), ("BE4", "#Q2", "#PY"), ("BEH", "#P0", "#LQ"), ("BEH", "#P2", "#LQ")])

    async def test_members_and_accounts_without_attacks(self):
        result = await self.read(war_type="REG", member_ids=[41, 42])
        self.assertEqual({row["player_tag"] for row in result["rows"]}, {"#P0", "#P2"})
        self.assertEqual(result["members_without_links"], [42])
        result = await self.read(account_tags=["#P2", "#V0"])
        self.assertEqual(({row["player_tag"] for row in result["rows"]}, result["accounts_without_attacks"]),
                         ({"#P2"}, ["#V0"]))

    async def test_missing_sources_and_failed_reads(self):
        result = await war_attacks.war_attacks(replace(self.context, bot=SimpleNamespace()),
                                               {"war_type": "CWL", **OCTOBER})
        self.assertEqual(result["cause"], "missing_setup")
        result = await war_attacks.war_attacks(replace(self.context, cwl_queries=None), {"war_type": "both", **OCTOBER})
        self.assertEqual(result["cause"], "missing_setup")
        with patch.object(WarReads, "selected_attacks", side_effect=OSError("locked")):
            result = await self.read()
        self.assertEqual(result, record_wars.UNAVAILABLE)
        result = await war_attacks.war_attacks(replace(self.context, account_links=None),
                                               {"war_type": "REG", "member_ids": [41], **OCTOBER})
        self.assertEqual(result["cause"], "missing_setup")
        # A war the record cannot find is named, not silently dropped.
        reads = self.context.bot.war_reads
        found = await reads.selected_attacks([("#PY", "CWL:#R9"), ("#PY", "REG:#PY:nonsense:#GR")])
        self.assertEqual((found["rows"], found["wars_checked"], len(found["wars_not_found"])), ([], 0, 2))

    async def test_a_war_missing_a_stored_attack_is_named_incomplete(self):
        await self.execute("DELETE FROM war_attacks WHERE attack_order=3 AND war_id="
                           "(SELECT id FROM wars WHERE war_tag='#R0')")
        result = await war_attacks.war_attacks(self.context, {"war_type": "CWL", "season": "2026-10"})
        self.assertEqual([(row["war_id"], row["player_tag"]) for row in result["rows"]], [("CWL:#R0", "#P0")])
        self.assertEqual(result["coverage"]["incomplete_wars"],
                         [{"war_id": "CWL:#R0", "clan_code": "BEH", "causes": ["attacks_incomplete"]}])

    async def test_access_is_refused_as_for_attendance(self):
        lost = AsyncMock(side_effect=AgentAccessLost("no access"))
        reader = AsyncMock()
        for name in ("read_war_attendance", "read_war_attacks"):
            with self.subTest(read=name), \
                    patch("elbow_helper.agent.reads.snapshots.require_evidence_access", lost), \
                    patch.object(war_attacks, "selected_wars", reader):
                with self.assertRaises(AgentAccessLost):
                    await TOOLS[name].handler(self.context, {"war_type": "REG", **OCTOBER})
        reader.assert_not_called()
        # Another member cannot continue the report, as with attendance.
        for name in ("read_war_attendance", "read_war_attacks"):
            with self.subTest(read=name):
                first = await TOOLS[name].handler(self.context, {"war_type": "REG", **OCTOBER})
                other = replace(self.context, member=SimpleNamespace(id=self.context.member.id + 1))
                with patch("elbow_helper.agent.reads.snapshots.require_evidence_access", AsyncMock()), \
                        patch("elbow_helper.agent.reads.snapshots.authorize_report", AsyncMock(return_value=True)):
                    refused = await TOOLS[name].handler(other, {"report_id": first["report_id"]})
                self.assertEqual(refused["cause"], "report_unavailable")

    async def test_the_tool_pages_and_exports_every_attack(self):
        tool = TOOLS["read_war_attacks"]
        first = await tool.handler(self.context, {"war_type": "both", **OCTOBER, "limit": 4})
        self.assertEqual((first["total_rows"], first["returned_rows"], first["complete_snapshot"]), (8, 4, True))
        self.assertEqual((first["selection"]["war_type"], first["provenance"]["required_access"]), ("both", []))
        rest = await tool.handler(self.context, {"report_id": first["report_id"], "cursor": first["next_cursor"],
                                                 "limit": 4})
        self.assertIsNone(rest["next_cursor"])
        direct = await self.read()
        self.assertEqual(first["rows"] + rest["rows"], direct["rows"])
        exported = await tool.handler(self.context, {"report_id": first["report_id"], "delivery": "csv",
                                                     "view": "count"})
        self.assertEqual((exported["artifact_status"], exported["artifact"]["total_rows"]), ("complete", 8))
        self.assertEqual(self.context.state.attachments[-1].filename, "war_attacks.csv")


if __name__ == "__main__":
    unittest.main()

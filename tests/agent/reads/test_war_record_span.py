"""War participation and attack reads say how far back the stored wars go (synthetic).

Wars are written into a family record. No Discord, Clash or LLM call is made.
"""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.agent.reads import record_wars, war_attacks, war_span, war_tools as war_reads, wars
from elbow_helper.data.reads import CwlReads, WarReads
from elbow_helper.features.cwl.queries import CwlQueries
from elbow_helper.features.cwl.record import CwlHistory

from tests.agent.engine.test_agent_plan_flow import _context
from tests.agent.reads.test_family_wars import renamed
from tests.agent.reads.test_record_wars import FAMILY, OCTOBER, later_war, ts
from tests.data.test_wars import END, SECOND, WarFixture, group, war

DAY = 86400 * SECOND
TOOLS = {tool.definition.name: tool for tool in war_reads.TOOLS}
JUNE_END = "20250610T180000.000Z"


def entries(result):
    if 'accounts' in result['record_span']:
        return {(row['player_tag'], row['war_type']): row for row in result['record_span']['accounts']}
    return {(entry['player_tag'], span['war_type']): {'player_tag': entry['player_tag'], **span}
            for row in result['rows'] for entry in row.get('family_wars', [])
            for span in entry.get('record_span', [])}


class RecordSpanTests(WarFixture, unittest.IsolatedAsyncioTestCase):
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
                        patch.object(war_span, "CLANS", FAMILY),
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
        # BEH: a regular war in June 2025, then two in late October 2026, all with #P0 and #P2.
        june = war("warEnded", end=JUNE_END)
        june.update(preparationStartTime="20250608T180000.000Z", startTime="20250609T180000.000Z")
        await self.record(june, at=ts(JUNE_END) * SECOND + 90 * SECOND)
        for payload, moment in ((war("warEnded"), END + 90 * SECOND), (later_war(), END + 2 * DAY + 90 * SECOND)):
            await self.record(payload, at=moment)
        # BE4: one regular war on 20 Oct for #Y0.
        be4 = renamed(war("warEnded", clans=("#LQ", "#CU"), end="20261020T180000.000Z"), P0="Y0", P2="Y2")
        be4.update(preparationStartTime="20261018T180000.000Z", startTime="20261019T180000.000Z")
        await self.record(be4, scope="#LQ", at=END + 3 * DAY)
        # BEH's league: one CWL war on 10 Oct with #P0 and #P2.
        league = war("warEnded", clans=("#PY", "#CU"), end="20261010T180000.000Z")
        league.update(preparationStartTime="20261008T180000.000Z", startTime="20261009T180000.000Z")
        await self.record(group(rounds=[{"warTags": ["#R0", "#R2"]}]), kind="cwl_group",
                          at=ts(league["preparationStartTime"]) * SECOND)
        await self.record(league, kind="cwl_war", scope="#R0", at=ts(league["endTime"]) * SECOND + 90 * SECOND)

    async def participation(self, **arguments):
        return await wars.war_participation(self.context, {"war_type": "REG", "clan_codes": ["BEH"], **arguments})

    async def test_a_clan_selection_states_the_record_span(self):
        result = await self.participation(latest_completed=1)
        span = result["record_span"]
        self.assertEqual(span["checked"], {"first_war_end": "2026-10-30", "last_war_end": "2026-10-30",
                                           "wars": 1, "period": "checked_wars"})
        self.assertEqual(span["recorded"], [{"war_type": "REG", "first_war_end": "2025-06-10",
                                             "last_war_end": "2026-10-30", "wars": 3}])
        self.assertNotIn("accounts", span)
        self.assertEqual((span["status"], set(span["meanings"])),
                         ("read", {"record_span", "checked", "recorded", "rows.family_wars.record_span"}))
        # Every family clan, both war types.
        family = await wars.war_participation(self.context, {"war_type": "both", **OCTOBER})
        self.assertEqual(family["record_span"]["recorded"], [
            {"war_type": "REG", "first_war_end": "2025-06-10", "last_war_end": "2026-10-30", "wars": 4},
            {"war_type": "CWL", "first_war_end": "2026-10-10", "last_war_end": "2026-10-10", "wars": 1}])
        self.assertEqual(family["record_span"]["checked"]["period"], "chosen_dates")

    async def test_wars_before_the_checked_window_are_counted(self):
        result = await self.participation(account_tags=["#P0"], **OCTOBER)
        self.assertEqual(entries(result)["#P0", "REG"], {
            "player_tag": "#P0", "war_type": "REG", "first_war_end": "2025-06-10", "last_war_end": "2026-10-30",
            "wars": 3, "wars_outside_checked": 1})
        self.assertEqual(result["record_span"]["checked"],
                         {"first_war_end": "2026-10-28", "last_war_end": "2026-10-30", "wars": 2,
                          "period": "chosen_dates"})
        # The latest war alone leaves the other two outside.
        latest = await self.participation(account_tags=["#P0"], latest_completed=1)
        self.assertEqual(entries(latest)["#P0", "REG"]["wars_outside_checked"], 2)
        # A season is its month.
        season = await self.participation(account_tags=["#P0"], season="2026-10")
        self.assertEqual(entries(season)["#P0", "REG"]["wars_outside_checked"], 1)
        # An account's wars in another family clan count, whichever clans were selected.
        moved = await self.participation(account_tags=["#Y0"], **OCTOBER)
        self.assertEqual(entries(moved)["#Y0", "REG"],
                         {"player_tag": "#Y0", "war_type": "REG", "first_war_end": "2026-10-20",
                          "last_war_end": "2026-10-20", "wars": 1, "wars_outside_checked": 0})

    async def test_both_war_types_and_members(self):
        result = await wars.war_participation(self.context, {"war_type": "both", "latest_completed": 2,
                                                          "unit": "member", "member_ids": [41]})
        found = entries(result)
        self.assertEqual(set(found), {("#P0", "REG"), ("#P0", "CWL"), ("#P2", "REG"), ("#P2", "CWL")})
        self.assertEqual((found["#P0", "REG"]["wars_outside_checked"], found["#P0", "CWL"]["wars_outside_checked"]),
                         (1, 1))
        self.assertEqual((found["#P0", "CWL"]["first_war_end"], found["#P0", "CWL"]["wars"]), ("2026-10-10", 1))
        # A checked account with no stored wars has an empty span, not repeated zero blocks.
        empty = await self.participation(account_tags=['#V0'], **OCTOBER)
        self.assertEqual(empty['record_span']['status'], 'read')
        self.assertEqual(empty['rows'][0]['family_wars'][0]['record_span'], [])

    async def test_the_selected_wars_are_unchanged(self):
        for arguments in ({"latest_completed": 1}, OCTOBER, {"season": "2026-10"}):
            with self.subTest(arguments=arguments):
                selection = {"war_type": "REG", "clan_codes": ["BEH"], "account_tags": ["#P0"], **arguments}
                plain = await war_reads.participation_totals(self.context, selection)
                spanned = await wars.war_participation(self.context, selection)
                self.assertEqual((spanned["war_ids"], spanned["clan_totals"]), (plain["war_ids"], plain["clan_totals"]))
                self.assertEqual([row["war_ids"] for row in spanned["rows"]], [row["war_ids"] for row in plain["rows"]])
        # No war selected and no dates chosen: every stored war is outside.
        none = await self.participation(account_tags=["#P0"], war_ids=["REG:#PY:20990101T000000.000Z:#GR"])
        self.assertEqual((none["war_ids"], none["record_span"]["checked"]["wars"]), ([], 0))
        self.assertEqual(entries(none)["#P0", "REG"]["wars_outside_checked"], 3)

    async def test_war_attacks_state_the_record_span(self):
        result = await war_attacks.war_attacks(self.context, {"war_type": "REG", "account_tags": ["#P0"], **OCTOBER})
        self.assertEqual({row["war_end_at"][:10] for row in result["rows"]}, {"2026-10-28", "2026-10-30"})
        self.assertEqual(entries(result)["#P0", "REG"]["wars_outside_checked"], 1)
        clans = await war_attacks.war_attacks(self.context, {"war_type": "REG", "latest_completed": 1})
        self.assertEqual(clans["record_span"]["recorded"][0]["first_war_end"], "2025-06-10")
        self.assertNotIn("accounts", clans["record_span"])
        # Members are read through their links.
        members = await war_attacks.war_attacks(self.context, {"war_type": "REG", "member_ids": [41], **OCTOBER})
        self.assertEqual({tag for tag, _ in entries(members)}, {"#P0", "#P2"})

    async def test_the_tools_return_it(self):
        for name in ("read_war_participation", "read_war_attacks"):
            with self.subTest(read=name):
                report = await TOOLS[name].handler(self.context, {"war_type": "REG", "account_tags": ["#P0"],
                                                                  "latest_completed": 1})
                self.assertEqual(entries(report)["#P0", "REG"]["wars_outside_checked"], 2)

    async def test_a_failed_or_missing_record_keeps_the_rest(self):
        selection = {"account_tags": ["#P0"], **OCTOBER}
        before = await self.participation(**selection)
        with patch.object(WarReads, "record_span", side_effect=OSError("locked")):
            failed = await self.participation(**selection)
        self.assertEqual(failed["record_span"], {"status": "unavailable", "cause": "source_unavailable"})
        def without_span(rows):
            return [{**row, 'family_wars': [{key: value for key, value in entry.items() if key != 'record_span'}
                                           for entry in row['family_wars']]} for row in rows]
        self.assertEqual(without_span(failed['rows']), without_span(before['rows']))
        with patch.object(WarReads, "record_span", side_effect=OSError("locked")):
            attacks = await war_attacks.war_attacks(self.context, {"war_type": "REG", **selection})
        self.assertEqual((attacks["record_span"]["status"], len(attacks["rows"])), ("unavailable", 2))
        # League wars read without the record's war reads; the span then has no source.
        league = await wars.war_participation(replace(self.context, bot=SimpleNamespace()),
                                           {"war_type": "CWL", "season": "2026-10", "account_tags": ["#P0"]})
        self.assertEqual(league["record_span"], {"status": "unavailable", "cause": "missing_setup"})
        self.assertEqual(len(league["rows"]), 1)

    async def test_accounts_beyond_the_old_header_limit_stay_on_their_rows(self):
        with patch.object(war_span, "MAX_ACCOUNTS", 1):
            result = await self.participation(account_tags=["#P0", "#P2", "#Z0"], **OCTOBER)
        # A tag that is not a Clash tag has no wars to read.
        self.assertEqual({tag for tag, _ in entries(result)}, {'#P0', '#P2'})
        self.assertNotIn('accounts_not_listed', result['record_span'])

    async def test_the_record_read_checks_its_bounds(self):
        reads = self.context.bot.war_reads
        with self.assertRaises(ValueError):
            await reads.record_span(["#PY"], windows=[(0, 1)] * 25)
        with self.assertRaises(ValueError):
            await reads.record_span(["#PY"], windows=[(2, 1)])
        with self.assertRaises(ValueError):
            await reads.record_span(["#PY"], player_tags=["#P0"] * 201, family=["#PY"])
        # A war first seen after as_of is not yet recorded.
        found = await reads.record_span(["#PY"], player_tags=["#P0"], family=["#PY", "#LQ"],
                                        as_of=ts(JUNE_END) * SECOND + 120 * SECOND)
        self.assertEqual(([row["wars"] for row in found["recorded"]], [row["outside"] for row in found["accounts"]]),
                         ([1], [1]))


if __name__ == "__main__":
    unittest.main()

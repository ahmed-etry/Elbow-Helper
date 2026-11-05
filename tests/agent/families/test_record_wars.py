"""The agent's war tools and joins read regular, league and current wars from the family record.

Synthetic wars are written through the collectors' own storage path into a
family record. No Discord, Clash or LLM call is made.
"""

from dataclasses import replace
from datetime import datetime, timezone
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.conversation.findings import report_is_current
from elbow_helper.agent.families import cohort_activity
from elbow_helper.agent.reads import projections, record_wars, war_tools as war_reads
from elbow_helper.agent.families.workflow_reads import TOOLS as WORKFLOW_TOOLS
from elbow_helper.agent.read_cache import ReadCache
from elbow_helper.configuration.channels import HIBERNATION_LOG
from elbow_helper.data.reads import CwlReads, WarReads
from elbow_helper.data.store.legacy_wars import map_legacy_war
from elbow_helper.features.cwl.queries import CwlQueries
from elbow_helper.features.cwl.record import CwlHistory
from elbow_helper.features.hibernation.queries import HibernationQueries
from elbow_helper.infrastructure.persistence.revisions import release_sqlite_revision

from agent.engine.test_agent_plan_flow import _context
from tests.data.test_wars import END, SECOND, WarFixture, group, war

DAY = 86400 * SECOND
FAMILY = {"BEH": SimpleNamespace(tag="#PY"), "BE4": SimpleNamespace(tag="#LQ")}
OCTOBER = {"after": "2026-10-01T00:00:00+00:00", "before": "2026-11-01T00:00:00+00:00"}
LATER_END = "20261030T180000.000Z"
TOOLS = {tool.definition.name: tool for tool in war_reads.TOOLS}


def later_war(state="warEnded"):
    payload = war(state, clans=("#PY", "#CU"), end=LATER_END)
    payload.update(preparationStartTime="20261028T180000.000Z", startTime="20261029T180000.000Z")
    return payload


def old_id(payload):
    return f"REG:#PY:{payload['endTime']}:{payload['opponent']['tag']}"


def ts(value):
    return int(datetime.strptime(value, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc).timestamp())


def comparable(result):
    """Per account: wars, attacks used and available, misses, stars and triples."""
    return {row["player_tag"]: (row["war_ids"], row["attacks_used"], row["attacks_expected"], row["missed"],
                                row["unconfirmed_opportunities"], row["participation_status"],
                                row["attack_performance"]["stars"], row["attack_performance"]["triples"],
                                row["attack_performance"]["observed_attacks"])
            for row in result["rows"]}


class RecordWarTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        for target in ('elbow_helper.infrastructure.clash.ClashClient.get',
                       'elbow_helper.infrastructure.ai.client.DeepSeekTextClient.complete',
                       'elbow_helper.infrastructure.ai.client.DeepSeekTextClient.create_agent_session'):
            patcher = patch(target, side_effect=AssertionError('War reads must not call Clash or the LLM'))
            forbidden = patcher.start()
            self.addCleanup(patcher.stop)
            self.addCleanup(forbidden.assert_not_called)
        self.now = END + 5 * DAY
        for patcher in (patch.object(record_wars, "CLANS", FAMILY),
                        patch.object(record_wars, "CODES", {"#PY": "BEH", "#LQ": "BE4"}),
                        patch.object(war_reads, "CLANS", FAMILY),
                        patch("elbow_helper.agent.reads.wars.CLANS", FAMILY),
                        patch("elbow_helper.data.reads.core.now_us", side_effect=lambda: self.now)):
            patcher.start()
            self.addCleanup(patcher.stop)
        links = SimpleNamespace(get_links_by_tags=lambda tags: {tag: {"discord_user_id": 41} for tag in tags},
                                get_links_for_members=lambda ids: {41: [{"player_tag": "#P0"}, {"player_tag": "#P2"}]})
        self.reads = WarReads(self.store)
        # League wars come from the same record, through the CWL queries.
        history = CwlHistory(CwlReads(self.store, FAMILY), None, FAMILY)
        self.context = replace(_context(), cwl_queries=CwlQueries(history, lambda: {}), account_links=links,
                               bot=SimpleNamespace(war_reads=self.reads))

    async def both_wars(self):
        """Two ended regular wars in the record."""
        for payload, at in ((war("warEnded"), END + 90 * SECOND), (later_war(), END + 2 * DAY + 90 * SECOND)):
            await self.record(payload, at=at)

    async def league_war(self, payload, war_tag):
        """A league round the collector saved: its group, then the war itself."""
        await self.record(group(rounds=[{"warTags": [war_tag, "#R2"]}]), kind="cwl_group",
                          at=ts(payload["preparationStartTime"]) * SECOND)
        await self.record(payload, kind="cwl_war", scope=war_tag, at=ts(payload["endTime"]) * SECOND + 90 * SECOND)

    async def attendance(self, arguments):
        return await war_reads.attendance(self.context, arguments)

    async def test_attendance_counts_each_recorded_war_and_keeps_coverage(self):
        await self.both_wars()
        arguments = {"war_type": "REG", "clan_codes": ["BEH"], **OCTOBER}
        new = await self.attendance(arguments)
        wars = [old_id(war("warEnded")), old_id(later_war())]
        self.assertEqual(comparable(new), {"#P0": (wars, 2, 4, 2, 0, "warring", 6, 2, 2),
                                           "#P2": (wars, 2, 4, 2, 0, "warring", 6, 2, 2)})
        self.assertEqual(new["war_ids"], wars)
        self.assertEqual((new["clan_totals"][0]["wars"], new["clan_totals"][0]["missed"]), (2, 4))
        coverage = new["coverage"]
        self.assertEqual((coverage["source"], coverage["regular_wars_not_checked_for_friendly"],
                          coverage["friendly_wars_not_counted"], coverage["wars_not_final"]),
                         ("family_record", 0, 0, 0))
        self.assertEqual({row["signal"] for row in coverage["source_status"]}, {"current_war", "war_log"})
        # The tool itself returns the same rows.
        report = await TOOLS["read_war_attendance"].handler(self.context, arguments)
        self.assertEqual(comparable(report), comparable(new))

    async def test_regular_wars_never_read_the_old_stored_wars(self):
        await self.both_wars()
        with patch.object(self.context.cwl_queries, "attendance", side_effect=AssertionError) as old:
            result = await self.attendance({"war_type": "REG", "clan_codes": ["BEH"], **OCTOBER})
        self.assertEqual(len(result["war_ids"]), 2)
        old.assert_not_called()

    async def test_without_the_record_reads_regular_and_current_wars_are_missing_setup(self):
        await self.both_wars()
        context = replace(self.context, bot=None)
        for kind in ("REG", "both"):
            result = await war_reads.attendance(context, {"war_type": kind, "clan_codes": ["BEH"], **OCTOBER})
            self.assertEqual(result["cause"], "missing_setup")
        self.assertEqual((await war_reads.current_war(context, {"clan_codes": ["BEH"]}))["cause"], "missing_setup")
        # League wars come through the CWL queries.
        self.assertNotIn("error", await war_reads.attendance(context, {"war_type": "CWL", "clan_codes": ["BEH"],
                                                                       **OCTOBER}))

    async def test_known_zero_stays_zero_and_a_missing_lineup_leaves_it_unknown(self):
        await self.both_wars()
        arguments = {"war_type": "REG", "clan_codes": ["BEH"], "account_tags": ["#Y0"], **OCTOBER}
        (row,) = (await self.attendance(arguments))["rows"]
        self.assertEqual((row["attacks_used"], row["missed"], row["participation_status"]),
                         (0, 0, "not_in_checked_lineups"))
        # A window without wars checked nothing; absence stays unknown.
        empty = {**arguments, "after": "2026-09-01T00:00:00+00:00", "before": "2026-10-01T00:00:00+00:00"}
        (row,) = (await self.attendance(empty))["rows"]
        self.assertEqual((row["attacks_used"], row["observation_status"]), (None, "no_stored_participation"))
        # A war the record knows without its lineup leaves every absence unknown.
        partial = war("warEnded", clans=("#PY", "#LQ"), end="20261020T180000.000Z")
        partial.update(preparationStartTime="20261018T180000.000Z", startTime="20261019T180000.000Z")
        partial["clan"].pop("members")
        await self.record(partial, at=END + 3 * DAY)
        result = await self.attendance(arguments)
        self.assertEqual(result["wars_without_rosters"], [old_id(partial)])
        self.assertIsNone(result["rows"][0]["attacks_used"])
        self.assertFalse(result["coverage"]["exact_ranking"])

    async def test_early_participation_wars_keep_known_misses_and_say_friendly_is_unchecked(self):
        early = war("warEnded")
        for member in early["clan"]["members"]:
            member.pop("townhallLevel")
            member.pop("mapPosition")
        await self.record(early, at=END + 90 * SECOND)
        # As the old copy writes them: no team size or times to judge friendly by.
        await self.execute("UPDATE wars SET team_size=NULL,type_basis='legacy_unchecked',preparation_at=NULL,start_at=NULL")
        result = await self.attendance({"war_type": "REG", "clan_codes": ["BEH"], **OCTOBER})
        self.assertEqual({row["player_tag"]: row["missed"] for row in result["rows"]}, {"#P0": 1, "#P2": 1})
        self.assertEqual(result["coverage"]["regular_wars_not_checked_for_friendly"], 1)
        self.assertEqual(result["wars_without_rosters"], [])
        source = await war_reads.selected_wars(self.context, {"clan_codes": ["BEH"], **OCTOBER}, "REG")
        self.assertEqual({row["start_ts"] for row in source["rows"]}, {None})
        self.assertEqual(source["war_aliases"][0]["type_basis"], "legacy_unchecked")

    async def test_an_attack_the_lineup_cannot_place_keeps_only_full_accounts_known(self):
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        # The log reports one attack more than the accounts add up to.
        await self.execute("UPDATE war_members SET attacks_used=2 WHERE player_tag='#P0'")
        await self.execute("UPDATE war_sides SET attacks=4 WHERE clan_tag='#PY'")
        source = await war_reads.selected_wars(self.context, {"clan_codes": ["BEH"], **OCTOBER}, "REG")
        rows = {row["player_tag"]: row for row in source["rows"]}
        self.assertEqual((rows["#P0"]["missed"], rows["#P0"]["final_capture"]), (0, True))
        self.assertEqual((rows["#P2"]["missed"], rows["#P2"]["attacks_expected"], rows["#P2"]["uncertainty_reasons"]),
                         (None, None, ["attacks_incomplete"]))

    async def test_friendly_wars_are_counted_apart(self):
        await self.both_wars()
        friendly = war("warEnded", clans=("#PY", "#LQ"), end="20261025T180000.000Z")
        friendly.update(preparationStartTime="20261024T170000.000Z", startTime="20261024T180000.000Z")
        await self.record(friendly, at=END + 3 * DAY)
        result = await self.attendance({"war_type": "REG", "clan_codes": ["BEH"], **OCTOBER})
        self.assertEqual(result["war_ids"], [old_id(war("warEnded")), old_id(later_war())])
        self.assertEqual(result["coverage"]["friendly_wars_not_counted"], 1)

    async def test_follow_up_by_record_or_old_ids_selects_the_same_war(self):
        await self.both_wars()
        first = old_id(war("warEnded"))
        # An old capture saw the end a second earlier; its ID stays an alias.
        earlier = first.replace("180000.000Z", "175959.000Z")
        war_id = (await self.rows("SELECT war_id FROM war_sides WHERE clan_tag='#GR'"))[0]["war_id"]
        await self.store.call(lambda c: (map_legacy_war(c, "clan_health", earlier, "BEH", war_id, at=self.now), c.commit()))
        for selected in ([first], [earlier], ["CWL:#R0", earlier], ["not an id"]):
            with self.subTest(selected=selected):
                result = await self.attendance({"war_type": "REG", "clan_codes": ["BEH"], "war_ids": selected})
                self.assertEqual(result["war_ids"], [first] if selected != ["not an id"] else [])
        self.assertIn(earlier, (await self.attendance({"war_type": "REG", "clan_codes": ["BEH"],
                                                              "war_ids": [first]}))["war_aliases"][0]["aliases"])

    async def test_latest_wars_and_both_kinds_rank_together(self):
        await self.both_wars()
        league = war("warEnded", clans=("#PY", "#LQ"), end="20261029T180000.000Z")
        await self.league_war(league, "#R0")
        latest = {"clan_codes": ["BEH"], "latest_completed": 1}
        self.assertEqual((await self.attendance({"war_type": "REG", **latest}))["war_ids"], [old_id(later_war())])
        for count, expected, used in ((2, [old_id(later_war()), "CWL:#R0"], 2),
                                      (3, [old_id(war("warEnded")), old_id(later_war()), "CWL:#R0"], 3)):
            new = await self.attendance({"war_type": "both", "clan_codes": ["BEH"], "latest_completed": count})
            self.assertEqual(sorted(new["war_ids"]), sorted(expected))
            self.assertEqual(comparable(new)["#P0"][1:4], (used, 2 * used, used))
        both = await self.attendance({"war_type": "both", "clan_codes": ["BEH"], **OCTOBER})
        self.assertEqual(set(both["coverage"]), {"query_complete", "historical_collection_complete", "exact_ranking",
                                                 "regular_wars_not_checked_for_friendly", "friendly_wars_not_counted",
                                                 "regular", "league", "basis"})
        self.assertEqual(comparable(both)["#P0"][:4], (sorted([*expected]), 3, 6, 3))

    async def test_record_failures_are_unavailable_not_empty(self):
        await self.both_wars()
        with patch.object(self.reads, "regular_selection", AsyncMock(side_effect=sqlite3.OperationalError("locked"))):
            result = await war_reads.selected_wars(self.context, {"clan_codes": ["BEH"], **OCTOBER}, "REG")
            self.assertEqual(result["cause"], "source_unavailable")
            tool = await TOOLS["read_war_attendance"].handler(
                self.context, {"war_type": "REG", "clan_codes": ["BEH"], **OCTOBER})
            self.assertEqual(tool["cause"], "source_unavailable")
        with patch.object(self.reads, "current", AsyncMock(side_effect=OSError)):
            result = await war_reads.current_war(self.context, {"clan_codes": ["BEH"]})
        self.assertEqual(result["cause"], "source_unavailable")
        # Recent seasons are a league selection, refused for regular wars as league history refuses them.
        refused = await war_reads.selected_wars(self.context, {"history_limit": 1}, "REG")
        self.assertEqual((refused["cause"], refused["error"]), ("invalid_arguments", "Invalid CWL history limit"))

    async def test_current_war_matches_the_clash_shape_and_unused_attacks_wait_for_the_end(self):
        for state, missed in (("inWar", None), ("warEnded", 1)):
            with self.subTest(state=state):
                payload = later_war(state)
                self.now += DAY
                await self.record(payload, at=self.now - 10 * SECOND)
                checked = {"items": [{"tag": "#PY", "status": "ok", "data": payload}]}
                with patch("elbow_helper.agent.reads.wars.current_wars", AsyncMock(return_value=checked)):
                    expected = await war_reads.current_war(self.context, {"clan_codes": ["BEH"], "include_attacks": True})
                new = await war_reads.current_war(self.context, {"clan_codes": ["BEH"], "include_attacks": True})
                (before,), (after,) = expected["rows"], new["rows"]
                self.assertEqual({key: value for key, value in after.items() if key != "observed_at"},
                                 {key: value for key, value in before.items() if key != "observed_at"})
                self.assertIsNotNone(after["observed_at"])
                self.assertEqual(after["members"][-1]["missed"], missed if state == "warEnded" else None)
        # An overdue check gives no war, as a failed live read did.
        self.now += 2 * DAY
        (stale,) = (await war_reads.current_war(self.context, {"clan_codes": ["BEH"]}))["rows"]
        self.assertEqual((stale["status"], stale["state"], stale["members"]), ("stale", None, []))
        # A check that found no war says so.
        await self.record({"state": "notInWar"}, at=self.now - 10 * SECOND)
        (idle,) = (await war_reads.current_war(self.context, {"clan_codes": ["BEH"]}))["rows"]
        self.assertEqual((idle["status"], idle["state"], idle["members"]), ("ok", "notInWar", []))

    async def test_a_new_record_write_makes_a_regular_war_report_not_current(self):
        await self.both_wars()
        context = replace(self.context, read_cache=ReadCache())
        self.addCleanup(release_sqlite_revision, self.path)
        tool = TOOLS["read_war_attendance"]
        with patch('elbow_helper.agent.tool_support.core.require_evidence_access', AsyncMock()):
            result = await tool.handler(context, {"war_type": "REG", "clan_codes": ["BEH"], **OCTOBER})
        self.assertEqual(set(result["source_versions"]), {"cwl_queries", "war_reads"})
        self.assertIsNotNone(result["source_versions"]["war_reads"])
        report = context.state.reports[result["report_id"]]
        self.assertTrue(report_is_current(context, report))
        await self.record(war("inWar", clans=("#PY", "#CU"), end="20261105T180000.000Z"), at=END + 6 * DAY)
        self.assertFalse(report_is_current(context, report))

    async def test_trial_and_cohort_joins_read_the_recorded_wars(self):
        await self.both_wars()
        selection = {"war_type": "REG", "clan_codes": ["BEH"], **OCTOBER}
        rows = [{"applicant_member_id": 41}]
        coverage = await projections.performance_projection(self.context, rows, selection)
        self.assertNotIn("error", coverage)
        self.assertEqual((rows[0]["war_attendance"]["attacks_expected"], rows[0]["war_attendance"]["missed"]), (8, 4))
        self.assertEqual(rows[0]["attack_performance"]["REG"]["account_war_opportunities"], 4)
        rows = [{"member_id": 41, "accounts": [{"player_tag": "#P0", "clan_code": "BEH"},
                                               {"player_tag": "#Y0", "clan_code": "BEH"}]}]
        await cohort_activity.activity_projection(self.context, rows, {"clan_codes": ["BEH"], **OCTOBER})
        self.assertEqual((rows[0]["war_activity"]["wars_observed"], rows[0]["war_activity"]["missed"]), (2, 2))

    async def test_hibernation_keeps_overlapping_war_attacks_unknown(self):
        await self.both_wars()
        # Hibernation began after the first war started and before the second did.
        started = ts("20261028T000000.000Z")
        context = self.context
        for role in context.member.roles:
            role.name = "Access"
            role.is_default = lambda: False
        channel = context.source_message.channel
        log = SimpleNamespace(**{**vars(channel), "id": HIBERNATION_LOG})
        context.guild.get_channel_or_thread = lambda identifier: log if identifier == HIBERNATION_LOG else channel
        # The request comes after both wars ended.
        message = SimpleNamespace(**{**vars(context.source_message), "created_at": datetime(2026, 11, 1, tzinfo=timezone.utc)})
        context = replace(context, source_message=message, hibernation_queries=HibernationQueries(
            lambda: {"41": {"hibernation_date": f"<t:{started}:F>"}}))
        tool = next(tool for tool in WORKFLOW_TOOLS if tool.definition.name == "read_hibernation")
        result = await tool.handler(context, {"include_activity": True, "activity_kinds": ["regular_war"]})
        self.assertNotIn("error", result)
        account = {account["player_tag"]: account for account in result["rows"][0]["accounts"]}["#P0"]
        self.assertIsNone(account["observed_regular_war_attacks_since_start"])
        self.assertEqual(account["observed_regular_war_attacks_in_post_start_wars"], 1)
        self.assertEqual([war["war_id"] for war in account["regular_war_overlapping_war_participation"]],
                         [old_id(war("warEnded"))])


if __name__ == "__main__":
    unittest.main()

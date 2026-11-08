"""The agent's war tools and joins read regular, league and current wars from the family record.

Synthetic wars are written through the collectors' own storage path into a
family record. No Discord, Clash or LLM call is made.
"""

from dataclasses import replace
from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.conversation.findings import report_is_current
from elbow_helper.agent.reads import cohort_activity
from elbow_helper.agent.reads import projections, record_wars, war_tools as war_reads
from elbow_helper.agent.reads.cache import ReadCache
from elbow_helper.data.reads import CwlReads, WarReads
from tests.data.record_fixtures import seed_war_alias
from elbow_helper.domain.war_attendance import aggregate_attendance
from elbow_helper.features.cwl.queries import CwlQueries
from elbow_helper.features.cwl.record import CwlHistory
from elbow_helper.infrastructure.persistence.revisions import release_sqlite_revision

from tests.agent.engine.test_agent_plan_flow import _context
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
        history = CwlHistory(CwlReads(self.store), None, FAMILY)
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
        await self.store.call(lambda c: (seed_war_alias(c, "clan_health", earlier, "BEH", war_id, at=self.now), c.commit()))
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

    async def running_league(self):
        """Clan BEH's round ended; clan BE4's first round is still in battle day, as Clash stores league wars."""
        await self.record(group(rounds=[{"warTags": ["#R0", "#R2"]}, {"warTags": ["#R8", "#R9"]}], season="2026-11"),
                          kind="cwl_group", at=self.now - 3 * DAY)
        await self.record(group(rounds=[{"warTags": ["#R0", "#R2"]}, {"warTags": ["#R8", "#R9"]}], season="2026-11"),
                          kind="cwl_group", scope="#LQ", at=self.now - 3 * DAY)
        ended = war("warEnded", end="20261101T180000.000Z")
        ended.update(preparationStartTime="20261030T180000.000Z", startTime="20261031T180000.000Z")
        await self.record(ended, kind="cwl_war", scope="#R0", at=ts(ended["endTime"]) * SECOND + 90 * SECOND)
        running = json.loads(json.dumps(war(clans=("#LQ", "#CU"), end="20261103T080000.000Z"))
                             .replace('"#P0"', '"#Y0"').replace('"#P2"', '"#Y2"'))
        running.update(preparationStartTime="20261101T080000.000Z", startTime="20261102T080000.000Z")
        running.pop("attacksPerMember")
        await self.record(running, kind="cwl_war", scope="#R8", at=self.now - 600 * SECOND)
        self.running = running

    async def league_checked(self, current=None):
        """Clan BE4's latest checks, as the collector makes them during its league."""
        await self.record(current or {"state": "notInWar"}, scope="#LQ", at=self.now - 10 * SECOND)
        await self.record(self.running, kind="cwl_war", scope="#R8", at=self.now - 10 * SECOND)

    async def be4(self):
        return (await TOOLS["read_current_war"].handler(self.context, {"clan_codes": ["BE4"]}))["rows"][0]

    async def test_current_war_shows_a_running_league_war_with_a_regular_wars_fields(self):
        await self.running_league()
        await self.league_checked()
        row = await self.be4()
        self.assertEqual((row["status"], row["state"], row["team_size"], row["opponent"], row["end_at"]),
                         ("ok", "inWar", 2, "Clan #CU", "20261103T080000.000Z"))
        self.assertEqual(row["score"]["clan"]["attacks"], 1)
        self.assertEqual([(m["player_tag"], m["attacks_used"], m["unused_attacks"], m["missed"]) for m in row["members"]],
                         [("#Y0", 1, 0, None), ("#Y2", 0, 1, None)])
        self.assertIsNotNone(row["observed_at"])
        await self.record(later_war("inWar"), at=self.now - 10 * SECOND)
        regular = (await TOOLS["read_current_war"].handler(self.context, {"clan_codes": ["BEH"]}))["rows"][0]
        self.assertEqual(set(row), set(regular))
        self.assertEqual(set(row["members"][0]), set(regular["members"][0]))
        # One account can be asked for, as in a regular war.
        one = await TOOLS["read_current_war"].handler(self.context, {"clan_codes": ["BE4"], "account_tags": ["#Y2"]})
        self.assertEqual([m["player_tag"] for m in one["rows"][0]["members"]], ["#Y2"])

    async def test_current_war_shows_preparation_once_the_battle_day_is_over(self):
        await self.running_league()
        next_round = json.loads(json.dumps(war("preparation", clans=("#LQ", "#GR"), end="20261104T080000.000Z"))
                                .replace('"#P0"', '"#Y0"').replace('"#P2"', '"#Y2"'))
        next_round.update(preparationStartTime="20261102T080000.000Z", startTime="20261103T080000.000Z")
        next_round.pop("attacksPerMember")
        await self.league_checked()
        await self.record(next_round, kind="cwl_war", scope="#R9", at=self.now - 10 * SECOND)
        self.assertEqual((await self.be4())["state"], "inWar")
        # The battle day passed; only the next round is checked from here.
        self.now = ts("20261103T080100.000Z") * SECOND
        await self.record({"state": "notInWar"}, scope="#LQ", at=self.now - 10 * SECOND)
        await self.record(next_round, kind="cwl_war", scope="#R9", at=self.now - 10 * SECOND)
        row = await self.be4()
        self.assertEqual((row["status"], row["state"], row["opponent"], row["end_at"]),
                         ("ok", "preparation", "Clan #GR", "20261104T080000.000Z"))
        self.assertEqual([m["unused_attacks"] for m in row["members"]], [1, 1])


    async def test_a_stale_league_check_gives_no_war_and_a_failed_read_is_unavailable(self):
        await self.running_league()
        await self.league_checked()
        # The clan's own check stays good, but the war's checks stopped.
        self.now += 900 * SECOND
        await self.record({"state": "notInWar"}, scope="#LQ", at=self.now - 10 * SECOND)
        row = await self.be4()
        self.assertEqual((row["status"], row["state"], row["members"]), ("stale", None, []))
        # A failed clan check shows nothing, league war or not.
        await self.league_checked()
        await self.record(None, scope="#LQ", status=503, at=self.now)
        row = await self.be4()
        self.assertEqual((row["status"], row["state"], row["members"]), ("unavailable", None, []))
        await self.league_checked()
        with patch.object(self.reads, "running_league", AsyncMock(side_effect=sqlite3.OperationalError("locked"))):
            result = await war_reads.current_war(self.context, {"clan_codes": ["BE4"]})
        self.assertEqual(result["cause"], "source_unavailable")

    def test_only_a_war_under_way_adds_unconfirmed_attacks_to_a_total(self):
        base = {"player_tag": "#P0", "player_name": "Player", "attacks_expected": None, "missed": None}
        rows = [{**base, "war_id": "running", "attacks_used": 1, "uncertainty_reasons": ["not_final"]},
                {**base, "war_id": "no count", "attacks_used": None, "uncertainty_reasons": ["not_final"]},
                {**base, "war_id": "ended", "attacks_used": 1, "uncertainty_reasons": ["attacks_incomplete"]},
                {**base, "war_id": "older", "attacks_used": 1}]
        (row,) = aggregate_attendance(rows, {}, "account")
        self.assertEqual((row["attacks_used"], row["attacks_expected"], row["missed"],
                          row["unconfirmed_opportunities"], row["totals_complete"]), (1, 0, 0, 4, False))


    async def test_another_clans_ended_round_never_hides_an_account_in_a_running_round(self):
        await self.running_league()
        season = {"season": "2026-11", "account_tags": ["#Y0", "#Y2", "#Y8"]}
        for name, arguments in (("read_war_attendance", {"war_type": "CWL", **season}),
                                ("read_war_attendance", {"war_type": "both", **season}),
                                ("read_cwl_results", {"metric": "attendance", **season})):
            with self.subTest(tool=name, arguments=arguments):
                result = await TOOLS[name].handler(self.context, arguments)
                rows = {row["player_tag"]: row for row in result["rows"]}
                self.assertEqual((rows["#Y0"]["war_ids"], rows["#Y0"]["participation_status"],
                                  rows["#Y0"]["attack_performance"]["observed_attacks"],
                                  rows["#Y0"]["unconfirmed_opportunities"], rows["#Y0"]["totals_complete"]),
                                 (["CWL:#R8"], "warring", 1, 1, False))
                # Attacks so far count; a running war has no misses and adds nothing owed.
                self.assertEqual((rows["#Y0"]["attacks_used"], rows["#Y0"]["attacks_expected"], rows["#Y0"]["missed"]),
                                 (1, 0, 0))
                self.assertEqual((rows["#Y2"]["attacks_used"], rows["#Y2"]["missed"]), (0, 0))
                self.assertEqual((rows["#Y2"]["war_ids"], rows["#Y2"]["participation_status"]),
                                 (["CWL:#R8"], "unknown"))
        # An account in none of the lineups is still a known zero, running wars checked too.
        result = await self.attendance({"war_type": "CWL", **season})
        self.assertEqual(result["coverage"]["wars_not_final"], 1)
        absent = next(row for row in result["rows"] if row["player_tag"] == "#Y8")
        self.assertEqual((absent["attacks_used"], absent["participation_status"]), (0, "not_in_checked_lineups"))


    async def test_a_running_war_without_a_lineup_leaves_absence_unknown(self):
        await self.running_league()
        await self.execute("DELETE FROM war_attacks WHERE war_id=(SELECT id FROM wars WHERE war_tag='#R8')")
        await self.execute("DELETE FROM war_members WHERE war_id=(SELECT id FROM wars WHERE war_tag='#R8')")
        result = await self.attendance({"war_type": "CWL", "season": "2026-11", "account_tags": ["#Y0"]})
        self.assertEqual(result["wars_without_rosters"], ["CWL:#R8"])
        self.assertEqual((result["rows"][0]["attacks_used"], result["rows"][0]["observation_status"]),
                         (None, "no_stored_participation"))

    async def test_scoring_and_cohort_selections_stay_completed_wars(self):
        await self.running_league()
        selected = await war_reads.selected_wars(self.context, {"season": "2026-11"}, "CWL")
        self.assertEqual(selected["war_ids"], ["CWL:#R0"])
        both = await war_reads.selected_wars(self.context, {"season": "2026-11"}, "both")
        self.assertEqual(both["war_ids"], ["CWL:#R0"])


    async def test_a_new_record_write_makes_a_regular_war_report_not_current(self):
        await self.both_wars()
        context = replace(self.context, read_cache=ReadCache())
        self.addCleanup(release_sqlite_revision, self.path)
        tool = TOOLS["read_war_attendance"]
        with patch('elbow_helper.agent.reads.snapshots.require_evidence_access', AsyncMock()):
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


if __name__ == "__main__":
    unittest.main()

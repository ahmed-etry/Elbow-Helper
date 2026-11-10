"""Chosen accounts' wars across every family clan, beside the clan-scoped attendance (synthetic).

Wars and clan stays are written into a family record. No Discord, Clash or LLM call is made.
"""

from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.agent.reads import record_wars, war_tools as war_reads, wars
from elbow_helper.data.reads import CwlReads, StayReads, WarReads
from elbow_helper.features.cwl.queries import CwlQueries
from elbow_helper.features.cwl.record import CwlHistory
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from tests.agent.engine.test_agent_plan_flow import _context
from tests.agent.reads.test_record_wars import FAMILY, OCTOBER, later_war, ts
from tests.data.test_wars import END, SECOND, WarFixture, group, war

DAY = 86400 * SECOND
TOOLS = {tool.definition.name: tool for tool in war_reads.TOOLS}
ZERO = {"wars": 0, "attacks_used": 0, "attacks_expected": 0, "missed": 0, "unconfirmed_opportunities": 0}
ARRIVED = datetime(2026, 10, 31, tzinfo=timezone.utc)
SEPTEMBER = datetime(2026, 9, 1, tzinfo=timezone.utc)


def at(moment):
    return int(moment.timestamp()) * SECOND


def renamed(payload, **tags):
    text = json.dumps(payload)
    for old, new in tags.items():
        text = text.replace(f'"#{old}"', f'"#{new}"')
    return json.loads(text)


class FamilyWarTests(WarFixture, unittest.IsolatedAsyncioTestCase):
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
                        patch("elbow_helper.data.reads.core.now_us", side_effect=lambda: self.now)):
            patcher.start()
            self.addCleanup(patcher.stop)
        links = SimpleNamespace(get_links_by_tags=lambda tags: {tag: {"discord_user_id": 41} for tag in tags},
                                get_links_for_members=lambda ids: {41: [{"player_tag": "#P0"}, {"player_tag": "#P2"}]})
        history = CwlHistory(CwlReads(self.store), None, FAMILY)
        self.context = replace(_context(), cwl_queries=CwlQueries(history, lambda: {}), account_links=links,
                               bot=SimpleNamespace(war_reads=WarReads(self.store), stay_reads=StayReads(self.store)))
        await self.seed()

    async def seed(self):
        # BEH: two regular wars for #P0 and #P2.
        for payload, moment in ((war("warEnded"), END + 90 * SECOND), (later_war(), END + 2 * DAY + 90 * SECOND)):
            await self.record(payload, at=moment)
        # BE4: one regular war on 20 Oct for #Y0, who joins BEH on 31 Oct.
        be4 = renamed(war("warEnded", clans=("#LQ", "#CU"), end="20261020T180000.000Z"), P0="Y0", P2="Y2")
        be4.update(preparationStartTime="20261018T180000.000Z", startTime="20261019T180000.000Z")
        await self.record(be4, scope="#LQ", at=END + 3 * DAY)
        # BEH's league: one CWL war for #C0, who plays no regular war.
        league = renamed(war("warEnded", clans=("#PY", "#CU"), end="20261010T180000.000Z"), P0="C0", P2="C2")
        league.update(preparationStartTime="20261008T180000.000Z", startTime="20261009T180000.000Z")
        await self.record(group(rounds=[{"warTags": ["#R0", "#R2"]}]), kind="cwl_group",
                          at=ts(league["preparationStartTime"]) * SECOND)
        await self.record(league, kind="cwl_war", scope="#R0", at=ts(league["endTime"]) * SECOND + 90 * SECOND)
        # Clan stays: #Y0 in BE4 until 31 Oct, then BEH; #C0 in BEH since September.
        await self.stay("#Y0", "#LQ", at(SEPTEMBER), at(ARRIVED), last=at(ARRIVED) - 3600 * SECOND)
        await self.stay("#Y0", "#PY", at(ARRIVED))
        await self.stay("#C0", "#PY", at(SEPTEMBER))

    async def stay(self, tag, clan, from_at, to_at=None, last=None):
        def write(connection):
            with sqlite_transaction(connection):
                for clan_tag, code in (("#PY", "BEH"), ("#LQ", "BE4")):
                    connection.execute("UPDATE clans SET family_code=NULL WHERE family_code=?", (code,))
                    connection.execute("INSERT INTO clans(tag,family_code,name,is_family,wiring_revision) "
                                       "VALUES (?,?,'Clan',1,1) ON CONFLICT(tag) DO UPDATE SET "
                                       "family_code=excluded.family_code,is_family=1", (clan_tag, code))
                connection.execute("INSERT OR IGNORE INTO accounts(player_tag,current_name,first_observed_at,"
                                   "tracking_status) VALUES (?,?,1,'observed')", (tag, tag))
                account = connection.execute("SELECT id FROM accounts WHERE player_tag=?", (tag,)).fetchone()[0]
                clan_id = connection.execute("SELECT id FROM clans WHERE tag=?", (clan,)).fetchone()[0]
                run = connection.execute("INSERT INTO collection_runs(collector,scope_kind,scheduled_at,started_at,"
                                         "status,expected) VALUES ('players','synthetic',1,1,'ok',0)").lastrowid
                item = connection.execute("INSERT INTO collection_items(run_id,ordinal,source_kind,status,started_at) "
                                          "VALUES (?,0,'synthetic','ok',1)", (run,)).lastrowid
                connection.execute(
                    "INSERT INTO clan_membership(account_id,clan_id,clan_role,from_at,to_at,last_observed_at,"
                    "end_previous_good_at,end_basis,source_item_id) VALUES (?,?,'member',?,?,?,?,?,?)",
                    (account, clan_id, from_at, to_at, last or from_at, last if to_at else None,
                     None if to_at is None else "observed_absence", item))
        await self.store.call(write)

    async def read(self, **arguments):
        return await wars.war_attendance(self.context, {"war_type": "REG", "clan_codes": ["BEH"], **OCTOBER,
                                                        **arguments})

    @staticmethod
    def family(result):
        return {entry["player_tag"]: entry for row in result["rows"] for entry in row.get("family_wars", [])}

    async def test_a_transfer_shows_its_wars_in_the_other_clan_and_its_arrival(self):
        result = await self.read(account_tags=["#Y0"])
        (row,) = result["rows"]
        # The clan-scoped figures stay as they were: no BEH war listed #Y0.
        self.assertEqual((row["participation_status"], row["attacks_used"]), ("not_in_checked_lineups", 0))
        entry = self.family(result)["#Y0"]
        self.assertEqual(entry["regular"], {"wars": 1, "attacks_used": 1, "attacks_expected": 2, "missed": 1,
                                            "unconfirmed_opportunities": 0})
        self.assertEqual(entry["cwl"], ZERO)
        self.assertEqual([(clan["clan_code"], clan["regular"]["wars"], clan["cwl"]["wars"]) for clan in entry["by_clan"]],
                         [("BE4", 1, 0)])
        self.assertEqual((entry["current_clan_code"], entry["in_current_clan_since"], entry["previous_family_clan_code"]),
                         ("BEH", ARRIVED.isoformat(), "BE4"))
        self.assertEqual(entry["previous_family_clan_last_seen_at"], datetime(2026, 10, 30, 23, tzinfo=timezone.utc).isoformat())
        scope = result["family_wars_scope"]
        self.assertEqual((scope["status"], scope["clan_codes"], scope["regular_wars"], scope["cwl_wars"],
                          scope["lineups_complete"], scope["clan_stays"]), ("read", ["BE4", "BEH"], 3, 1, True, "read"))
        self.assertIn("family_wars", scope["meanings"])

    async def test_a_cwl_only_account_shows_cwl_and_zero_regular(self):
        entry = self.family(await self.read(account_tags=["#C0"]))["#C0"]
        self.assertEqual(entry["regular"], ZERO)
        self.assertEqual((entry["cwl"]["wars"], entry["cwl"]["attacks_used"]), (1, 1))
        self.assertEqual([(clan["clan_code"], clan["regular"], clan["cwl"]["wars"]) for clan in entry["by_clan"]],
                         [("BEH", ZERO, 1)])
        self.assertEqual((entry["current_clan_code"], entry["previous_family_clan_code"]), ("BEH", None))

    async def test_an_account_with_neither_shows_both_zero(self):
        entry = self.family(await self.read(account_tags=["#V0"]))["#V0"]
        self.assertEqual((entry["regular"], entry["cwl"], entry["combined"], entry["by_clan"]), (ZERO, ZERO, ZERO, []))
        self.assertEqual((entry["current_clan_code"], entry["in_current_clan_since"],
                          entry["previous_family_clan_code"]), (None, None, None))

    async def test_combined_figures_equal_the_parts(self):
        result = await self.read(war_type="both", account_tags=["#P0", "#Y0", "#C0", "#V0"])
        entries = self.family(result)
        self.assertEqual(set(entries), {"#P0", "#Y0", "#C0", "#V0"})
        for tag, entry in entries.items():
            with self.subTest(tag=tag):
                self.assertEqual(entry["combined"], {key: entry["regular"][key] + entry["cwl"][key] for key in ZERO})
                for kind in ("regular", "cwl"):
                    self.assertEqual(entry[kind], {key: sum(clan[kind][key] for clan in entry["by_clan"]) for key in ZERO})
        self.assertEqual(entries["#P0"]["combined"]["wars"], 2)

    async def test_clan_scoped_figures_are_unchanged(self):
        arguments = {"war_type": "both", "clan_codes": ["BEH"], "account_tags": ["#P0", "#Y0", "#C0"], **OCTOBER}
        plain = await war_reads.attendance(self.context, arguments)
        family = await wars.war_attendance(self.context, arguments)
        self.assertEqual([{key: value for key, value in row.items() if key != "family_wars"} for row in family["rows"]],
                         plain["rows"])
        self.assertEqual({key: value for key, value in family.items() if key not in ("rows", "family_wars_scope")},
                         {key: value for key, value in plain.items() if key != "rows"})

    async def test_a_season_reads_that_month_and_its_league(self):
        entries = self.family(await wars.war_attendance(self.context, {
            "war_type": "REG", "clan_codes": ["BEH"], "season": "2026-10", "account_tags": ["#Y0", "#C0"]}))
        self.assertEqual((entries["#Y0"]["regular"]["wars"], entries["#C0"]["cwl"]["wars"]), (1, 1))

    async def test_members_get_one_entry_per_account(self):
        result = await self.read(unit="member", member_ids=[41])
        (row,) = result["rows"]
        self.assertEqual([entry["player_tag"] for entry in row["family_wars"]], ["#P0", "#P2"])
        self.assertEqual(row["family_wars"][0]["regular"]["wars"], 2)

    async def test_whole_clan_reads_and_war_subsets_add_nothing_per_account(self):
        result = await self.read()
        self.assertNotIn("family_wars_scope", result)
        self.assertFalse(any("family_wars" in row for row in result["rows"]))
        subset = await wars.war_attendance(self.context, {"war_type": "REG", "clan_codes": ["BEH"],
                                                          "latest_completed": 1, "account_tags": ["#P0"]})
        self.assertEqual(subset["family_wars_scope"]["status"], "not_read")
        self.assertFalse(any("family_wars" in row for row in subset["rows"]))
        # The tool reads the same way.
        report = await TOOLS["read_war_attendance"].handler(self.context, {
            "war_type": "REG", "clan_codes": ["BEH"], "account_tags": ["#Y0"], **OCTOBER})
        self.assertEqual(report["family_wars_scope"]["status"], "read")

    async def test_an_unavailable_source_keeps_the_clan_figures(self):
        # Without the league source, the family read fails but the clan's regular wars still come back.
        result = await wars.war_attendance(replace(self.context, cwl_queries=None), {
            "war_type": "REG", "clan_codes": ["BEH"], "account_tags": ["#P0"], **OCTOBER})
        self.assertEqual(result["family_wars_scope"], {"status": "unavailable", "cause": "missing_setup"})
        self.assertEqual((result["rows"][0]["attacks_used"], "family_wars" in result["rows"][0]), (2, False))
        # Without clan stays, the war figures stay and the stay fields are left out.
        result = await wars.war_attendance(replace(self.context, bot=SimpleNamespace(war_reads=self.context.bot.war_reads)),
                                           {"war_type": "REG", "clan_codes": ["BEH"], "account_tags": ["#Y0"], **OCTOBER})
        entry = self.family(result)["#Y0"]
        self.assertEqual(result["family_wars_scope"]["clan_stays"], "unavailable")
        self.assertEqual(entry["regular"]["wars"], 1)
        self.assertNotIn("current_clan_code", entry)

    async def test_a_failed_stay_read_is_unavailable(self):
        with patch.object(StayReads, "stays", side_effect=OSError("locked")):
            result = await self.read(account_tags=["#Y0"])
        self.assertEqual(result["family_wars_scope"]["clan_stays"], "unavailable")
        # A tag that is not a Clash tag has no stays; the others are still read.
        entries = self.family(await self.read(account_tags=["#Y0", "#Z0"]))
        self.assertEqual((entries["#Y0"]["current_clan_code"], entries["#Z0"]["current_clan_code"]), ("BEH", None))

    def test_a_stay_copied_into_the_record_counts_from_its_first_capture(self):
        hour = 3600 * SECOND
        copied = {"clan_code": "BE4", "from_at": 10 * hour, "to_at": 20 * hour, "first_seen_at": 10 * hour,
                  "last_seen_at": 20 * hour, "basis": "observed_hourly"}
        carried = {"clan_code": "BE4", "from_at": 21 * hour, "to_at": 30 * hour, "first_seen_at": 21 * hour,
                   "last_seen_at": 29 * hour, "basis": "record"}
        moved = {"clan_code": "BEH", "from_at": 30 * hour, "to_at": None, "first_seen_at": 30 * hour,
                 "last_seen_at": 40 * hour, "basis": "record"}
        facts = wars.stay_facts([copied, carried, moved])
        self.assertEqual((facts["current_clan_code"], facts["previous_family_clan_code"],
                          facts["previous_family_clan_last_seen_at"]),
                         ("BEH", "BE4", datetime.fromtimestamp(29 * 3600, timezone.utc).isoformat()))
        # Left the family: no current clan, and the last clan it was in.
        facts = wars.stay_facts([copied, {**carried, "to_at": 30 * hour}])
        self.assertEqual((facts["current_clan_code"], facts["previous_family_clan_code"]), (None, "BE4"))
        # One merged stay still open: the copy's first capture is when it was first listed.
        facts = wars.stay_facts([copied, {**carried, "to_at": None}])
        self.assertEqual((facts["in_current_clan_since"], facts["previous_family_clan_code"]),
                         (datetime.fromtimestamp(10 * 3600, timezone.utc).isoformat(), None))


if __name__ == "__main__":
    unittest.main()

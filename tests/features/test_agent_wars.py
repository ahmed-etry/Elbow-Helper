from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.reports.roles import RoleAccountReport
from elbow_helper.features.agent.tools.wars import (
    list_regular_war_status, read_regular_war, read_regular_war_report,
)
from elbow_helper.features.agent.reports.war import RegularWarReport
from elbow_helper.features.wars.helpers import build_war_id
from elbow_helper.features.wars.queries import WarQueries


def _war(state="inWar"):
    return {
        "state": state, "teamSize": 2, "attacksPerMember": 2,
        "preparationStartTime": "20260915T000000.000Z",
        "startTime": "20260916T000000.000Z",
        "endTime": "20260917T000000.000Z",
        "clan": {"tag": "#P0", "name": "Hellbow", "stars": 3,
                 "destructionPercentage": 50.0, "members": [
                     {"tag": "#P0", "name": "Alpha", "townhallLevel": 18,
                      "mapPosition": 1,
                      "attacks": [{"stars": 3, "destructionPercentage": 100}]},
                     {"tag": "#P2", "name": "Beta", "townhallLevel": 17,
                      "mapPosition": 2, "attacks": []},
                 ]},
        "opponent": {"tag": "#P8", "name": "Opponent", "stars": 2,
                     "destructionPercentage": 40.0, "members": []},
    }


class AgentWarToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        current = _war("inWar")
        previous = _war("warEnded")
        manager = SimpleNamespace(
            war_board_history={"BEH": {"current": current, "previous": previous}},
            war_observations={"BEH": {
                "state": "inwar", "observed_at": "2026-09-17T00:00:00+00:00",
                "war_id": build_war_id(current),
            }},
        )
        member = SimpleNamespace(id=42, roles=[SimpleNamespace(id=next(iter(CORE)))])
        guild = SimpleNamespace(id=1, me=member, get_member=lambda _: member)
        channel = SimpleNamespace(
            id=100, guild=guild,
            permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True),
        )
        guild.get_channel_or_thread = lambda value: channel if value == 100 else None
        self.context = AgentRequestContext(
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)),
            guild=guild, member=member,
            source_message=SimpleNamespace(channel=channel, created_at=datetime.now(timezone.utc)),
            account_links=None, clan_health=None, message_search=None,
            war_queries=WarQueries(manager),
        )

    async def test_status_list_does_not_poll_and_preserves_unavailable_clans(self):
        result = await list_regular_war_status(self.context, {})
        self.assertFalse(result["poll_performed"])
        statuses = {row["clan_code"]: row["evidence_status"] for row in result["clans"]}
        self.assertEqual(statuses["BEH"], "observed")
        self.assertEqual(statuses["BE4"], "unavailable")

    async def test_current_report_retains_complete_roster_and_pages_without_reread(self):
        first = await read_regular_war(self.context, {"clan_code": "BEH"})
        self.assertEqual(first["attack_totals"]["remaining"], 3)
        self.assertEqual(first["attack_totals"]["missed"], 0)
        self.assertIsInstance(self.context.state.reports[first["report_id"]], RegularWarReport)
        self.context.war_queries._manager.war_board_history.clear()
        page = await read_regular_war_report(self.context, {
            "report_id": first["report_id"], "offset": 1, "limit": 1,
        })
        self.assertEqual(page["members"][0]["player_name"], "Beta")
        self.assertEqual(page["war_id"], first["war_id"])

    async def test_previous_ended_report_has_missed_attack_totals(self):
        result = await read_regular_war(
            self.context, {"clan_code": "BEH", "selected": "previous"},
        )
        self.assertEqual(result["state"], "warended")
        self.assertEqual(result["attack_totals"]["missed"], 3)
        self.assertEqual(result["attack_totals"]["players_with_missed_attacks"], 2)

    async def test_not_in_war_and_invalid_cached_data_fail_without_artifacts(self):
        unsupported = await read_regular_war(self.context, {"clan_code": "NOPE"})
        self.assertIn("error", unsupported)
        manager = self.context.war_queries._manager
        manager.war_observations["BEH"] = {
            "state": "notinwar", "observed_at": "now", "war_id": None,
        }
        result = await read_regular_war(self.context, {"clan_code": "BEH"})
        self.assertEqual(result["evidence_status"], "not_in_war")
        self.assertIsNone(result["report_id"])
        self.assertEqual(self.context.state.reports, {})

        manager.war_observations.clear()
        manager.war_board_history["BEH"]["current"]["clan"]["members"][0]["tag"] = "bad"
        result = await read_regular_war(self.context, {"clan_code": "BEH"})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.reports, {})

    async def test_access_loss_and_report_budget_fail_closed(self):
        with patch("elbow_helper.features.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES", 1):
            result = await read_regular_war(self.context, {"clan_code": "BEH"})
        self.assertIn("error", result)
        self.assertEqual(self.context.state.reports, {})
        self.context.member.roles = []
        with self.assertRaises(AgentAccessLost):
            await list_regular_war_status(self.context, {})

    async def test_report_reads_reject_missing_wrong_kind_and_foreign_guild(self):
        first = await read_regular_war(self.context, {"clan_code": "BEH"})
        report = self.context.state.reports[first["report_id"]]
        self.context.state.reports["role"] = RoleAccountReport("role", "now", (), ())
        self.context.state.reports["foreign"] = RegularWarReport(
            "foreign", 2, report.snapshot,
        )
        for report_id in ("missing", "role", "foreign"):
            result = await read_regular_war_report(
                self.context, {"report_id": report_id},
            )
            self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()

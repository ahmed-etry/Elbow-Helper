from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.roles import CO_APPLICANT_ROLE_ID, CORE, LEAD
from elbow_helper.features.agent.access import ACCESS_LEAD, AgentAccessLost
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.tools.events import (
    read_event_schedule, read_event_schedule_report,
)
from elbow_helper.features.event_stats.queries import (
    EventScheduleRow, EventScheduleSnapshot,
)


class _Queries:
    def snapshot(self, guild):
        return EventScheduleSnapshot(
            "2026-09-19T12:00:00+00:00",
            (
                EventScheduleRow(
                    "members", "Members", "preset", "counter", "counter",
                    True, "live", 0, None, 0, None, None, None,
                    20, 2, 0, "complete",
                ),
                EventScheduleRow(
                    "cwl", "CWL", "preset", "recurring", "range",
                    True, "upcoming", 1, None, 24,
                    "2026-10-01T08:00:00+00:00",
                    "2026-10-09T08:00:00+00:00", None,
                    None, 0, 0, None,
                ),
            ),
        )


class AgentEventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.member = SimpleNamespace(
            id=42, roles=[
                SimpleNamespace(id=next(iter(CORE))),
                SimpleNamespace(id=next(iter(LEAD))),
            ],
        )
        self.bot_member = SimpleNamespace(id=99, roles=[])
        self.channel = SimpleNamespace(
            id=100,
            permissions_for=lambda _: SimpleNamespace(
                view_channel=True, read_message_history=True,
            ),
        )
        self.guild = SimpleNamespace(
            id=1, me=self.bot_member,
            get_member=lambda value: self.member if value == 42 else self.bot_member,
            get_channel_or_thread=lambda value: self.channel if value == 100 else None,
        )
        self.channel.guild = self.guild
        self.context = AgentRequestContext(
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)),
            guild=self.guild, member=self.member,
            source_message=SimpleNamespace(channel=self.channel),
            account_links=None, clan_health=None, message_search=None,
            event_queries=_Queries(),
        )

    async def test_event_schedule_is_role_scoped_retained_and_filtered(self):
        first = await read_event_schedule(self.context, {
            "event_type": "recurring",
        })

        self.assertEqual(first["event_count"], 2)
        self.assertEqual(first["matching_rows"], 1)
        self.assertEqual(first["events"][0]["event_key"], "cwl")
        self.assertEqual(self.context.state.required_access, {ACCESS_LEAD})
        retained = await read_event_schedule_report(self.context, {
            "report_id": first["report_id"], "phase": "live",
        })
        self.assertEqual(retained["events"][0]["member_count"], 20)

    async def test_non_lead_fails_before_read(self):
        self.member.roles = [
            SimpleNamespace(id=next(iter(CORE))),
            SimpleNamespace(id=CO_APPLICANT_ROLE_ID),
        ]
        self.context.event_queries.snapshot = unittest.mock.Mock()

        with self.assertRaises(AgentAccessLost):
            await read_event_schedule(self.context, {})

        self.context.event_queries.snapshot.assert_not_called()

    async def test_lead_loss_during_read_retains_nothing(self):
        with patch(
            "elbow_helper.features.agent.tools.events.require_access_requirements",
            side_effect=[None, AgentAccessLost("revoked")],
        ):
            with self.assertRaises(AgentAccessLost):
                await read_event_schedule(self.context, {})

        self.assertEqual(self.context.state.reports, {})
        self.assertEqual(self.context.state.required_access, set())

    async def test_lead_loss_hides_retained_report(self):
        first = await read_event_schedule(self.context, {})
        self.member.roles = [SimpleNamespace(id=next(iter(CORE)))]

        with self.assertRaises(AgentAccessLost):
            await read_event_schedule_report(self.context, {
                "report_id": first["report_id"],
            })


if __name__ == "__main__":
    unittest.main()

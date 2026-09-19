from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.configuration.channels import OVERSEEING_TERRACE
from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.tools.member_lifecycle import (
    read_member_lifecycle, read_member_lifecycle_report,
)
from elbow_helper.features.member_lifecycle.queries import MemberLifecycleQueries


class AgentMemberLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.permissions = {
            50: True,
            OVERSEEING_TERRACE: True,
            200: True,
            201: False,
        }
        self.member = SimpleNamespace(
            id=42, display_name="Requester",
            roles=[SimpleNamespace(id=next(iter(CORE)))],
        )
        self.observed = SimpleNamespace(id=7, display_name="Observed", roles=[])
        self.hidden = SimpleNamespace(id=8, display_name="Hidden", roles=[])
        self.bot_member = SimpleNamespace(id=99, roles=[])
        self.guild = SimpleNamespace(id=1, me=self.bot_member)

        def channel(channel_id):
            if channel_id not in self.permissions:
                return None
            value = SimpleNamespace(
                id=channel_id, guild=self.guild,
                permissions_for=lambda _: SimpleNamespace(
                    view_channel=self.permissions[channel_id],
                    read_message_history=self.permissions[channel_id],
                ),
            )
            return value

        self.guild.members = [self.member, self.observed, self.hidden]
        self.guild.get_member = lambda value: {
            42: self.member, 7: self.observed, 8: self.hidden, 99: self.bot_member,
        }.get(value)
        self.guild.get_channel_or_thread = channel
        self.source = channel(50)
        state = {
            "members": {
                "7": {
                    "platform": "Reddit",
                    "joined_at_iso": "2026-09-18T10:00:00+00:00",
                    "left": False,
                },
                "8": {
                    "platform": "Discord",
                    "joined_at_iso": "2026-09-17T10:00:00+00:00",
                    "left": False,
                },
            },
            "last_seen": {
                "7": {"ts_iso": "2026-09-19T09:00:00+00:00", "channel_id": 200},
                "8": {"ts_iso": "2026-09-19T08:00:00+00:00", "channel_id": 201},
            },
            "platform_counts": {"Reddit": 1},
            "overdue_applicant_ids": [8],
            "last_weekly_report_iso": None,
            "last_applicant_scan_iso": "2026-09-19T06:00:00+00:00",
        }
        queries = MemberLifecycleQueries(
            lambda: state,
            clock=lambda: datetime(2026, 9, 19, 12, tzinfo=timezone.utc),
        )
        self.context = AgentRequestContext(
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)),
            guild=self.guild, member=self.member,
            source_message=SimpleNamespace(channel=self.source),
            account_links=None, clan_health=None, message_search=None,
            member_lifecycle_queries=queries,
        )

    async def test_snapshot_filters_activity_by_channel_access_and_retains_sources(self):
        first = await read_member_lifecycle(self.context, {})

        self.assertEqual(first["tracked_current_member_count"], 2)
        self.assertEqual(first["untracked_current_member_count"], 1)
        rows = {row["member_id"]: row for row in first["members"]}
        self.assertEqual(rows[7]["last_seen_channel_id"], 200)
        self.assertIsNone(rows[8]["last_seen_channel_id"])
        self.assertEqual(
            self.context.state.source_channels,
            {OVERSEEING_TERRACE, 200},
        )

        retained = await read_member_lifecycle_report(self.context, {
            "report_id": first["report_id"], "overdue_only": True,
        })
        self.assertEqual([row["member_id"] for row in retained["members"]], [8])

    async def test_overseeing_access_is_required_before_state_read(self):
        self.permissions[OVERSEEING_TERRACE] = False
        result = await read_member_lifecycle(self.context, {})
        self.assertEqual(result, {"error": "The member lifecycle source is not accessible."})
        self.assertEqual(self.context.state.reports, {})

    async def test_activity_permission_loss_hides_retained_snapshot(self):
        first = await read_member_lifecycle(self.context, {})
        self.permissions[200] = False

        with self.assertRaises(AgentAccessLost):
            await read_member_lifecycle_report(self.context, {
                "report_id": first["report_id"],
            })


if __name__ == "__main__":
    unittest.main()

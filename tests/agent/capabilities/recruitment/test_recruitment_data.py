"""Recruitment workflow reads come from live state and keep ticket visibility."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.access import AgentAccessLost
from elbow_helper.agent.datasets.catalogue import SOURCES
from elbow_helper.agent.datasets.guide import DataGuide
from elbow_helper.agent.families import workflow_reads
from elbow_helper.agent.models import AgentTurnState
from elbow_helper.features.recruitment.queries import (
    RecruitmentQueries, RecruitmentTicketReminder, RecruitmentTrialFollowup,
)


REMINDERS = {
    "500": {"applicant_id": 50, "created_at": 1792530000, "resolved_at": 1792640000,
            "resolved_by": 10, "channel_id": 800, "message_id": 501, "_case_started_at": 1},
    "600": {"applicant_id": 60, "created_at": 1792530000, "resolved_at": None,
            "resolved_by": None, "channel_id": 800, "message_id": 601},
    "bad": {"applicant_id": 70},
}
ACTIVITY = {
    "600": {"last_reminder_at": "2026-10-21T17:00:00+00:00", "message_id": None,
            "message_channel_id": None, "last_applicant_message_id": 602},
    "700": {"last_reminder_at": "2026-10-20T17:00:00+00:00", "message_id": 9,
            "message_channel_id": 8, "last_applicant_message_id": None},
}


class RecruitmentDataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = {"reminders": REMINDERS, "activity": ACTIVITY}
        self.queries = RecruitmentQueries(
            dict, reminder_loader=lambda: self.state["reminders"],
            ticket_activity_loader=lambda: self.state["activity"])

    def context(self):
        channels = [SimpleNamespace(id=identifier) for identifier in (500, 600, 700)]
        return SimpleNamespace(guild=SimpleNamespace(channels=channels, threads=[]),
                               member=SimpleNamespace(id=10), state=AgentTurnState(),
                               recruitment_queries=self.queries)

    def test_typed_reads_project_only_documented_fields(self):
        self.assertEqual(self.queries.trial_followups(), (
            RecruitmentTrialFollowup(500, 50, 1792530000, 1792640000, 501),
            RecruitmentTrialFollowup(600, 60, 1792530000, None, 601),
        ))
        self.assertEqual(self.queries.ticket_reminders(), (
            RecruitmentTicketReminder(600, "2026-10-21T17:00:00+00:00", 602),
            RecruitmentTicketReminder(700, "2026-10-20T17:00:00+00:00", None),
        ))
        self.state["reminders"] = []
        with self.assertRaises(ValueError):
            self.queries.trial_followups()

    async def test_rows_keep_only_tickets_the_asker_can_open_and_recheck_access(self):
        context = self.context()
        revoked = False

        async def check_access(context):
            if 600 in context.state.source_channels and revoked:
                raise AgentAccessLost("Ticket access lost")

        with (
            patch.object(workflow_reads, "require_evidence_access", check_access),
            patch.object(workflow_reads, "can_access_message_channel",
                         AsyncMock(side_effect=lambda _, channel, **kw: channel.id != 700)),
        ):
            result = await workflow_reads.ticket_records(context, "ticket_reminders")
            self.assertEqual([row["ticket_channel_id"] for row in result["rows"]], [600])
            self.assertEqual(context.state.source_channels, {600})
            result = await workflow_reads.ticket_records(context, "trial_followups")
            self.assertEqual([row["ticket_channel_id"] for row in result["rows"]], [500, 600])
            self.assertEqual(result["rows"][0]["trial_ended_at"], 1792640000)
            context.state.source_channels.clear()
            revoked = True
            with self.assertRaises(AgentAccessLost):
                await workflow_reads.ticket_records(context, "ticket_reminders")

    async def test_invalid_or_missing_state_is_unavailable_not_empty_history(self):
        context = self.context()
        self.state["reminders"] = []
        with patch.object(workflow_reads, "require_evidence_access", AsyncMock()):
            result = await workflow_reads.ticket_records(context, "trial_followups")
            self.assertIn("error", result)
            context.recruitment_queries = None
            result = await workflow_reads.ticket_records(context, "trial_followups")
            self.assertEqual(result["cause"], "missing_setup")

    def test_no_recruitment_file_remains_an_sql_source_and_the_guide_names_the_read(self):
        self.assertFalse([source for source in SOURCES
                          if (source.state_name or "").startswith("recruitment")])
        guide = DataGuide(None, sources=())
        visible = guide.for_levels({"recruitment"})
        self.assertIn("read_recruitment_pipeline returns active trials", visible)
        self.assertIn("not a new accept/decline stage", visible)
        self.assertNotIn("state.recruitment", visible)
        self.assertNotIn("read_recruitment_pipeline", guide.for_levels(set()))


if __name__ == "__main__":
    unittest.main()

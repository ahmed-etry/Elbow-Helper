"""Recruitment workflow reads come from live state and keep ticket visibility."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.reads import workflow_reads
from elbow_helper.agent.engine.models import AgentTurnState
from elbow_helper.features.recruitment.queries import RecruitmentQueries, RecruitmentTicketReminder, RecruitmentTrialFollowup


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


    async def test_invalid_or_missing_state_is_unavailable_not_empty_history(self):
        context = self.context()
        self.state["reminders"] = []
        with patch.object(workflow_reads, "require_evidence_access", AsyncMock()):
            result = await workflow_reads.ticket_records(context, "trial_followups")
            self.assertIn("error", result)
            context.recruitment_queries = None
            result = await workflow_reads.ticket_records(context, "trial_followups")
            self.assertEqual(result["cause"], "missing_setup")


if __name__ == "__main__":
    unittest.main()

"""Recruitment state joins preserve lifecycle meaning and ticket visibility."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.capabilities.recruitment.data import SOURCES
from elbow_helper.agent.datasets.guide import DataGuide
from elbow_helper.agent.datasets.query import query_context, run_query
from elbow_helper.agent.models import AgentTurnState
from elbow_helper.agent.access import AgentAccessLost


class RecruitmentDataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.paths = SimpleNamespace(project_root=Path(directory.name))
        self.docs = {
            "recruitment_trials": {
                str(ticket): {
                    "applicant_id": member, "start": "2026-10-21T17:00:00+00:00",
                    "days": 7, "tracking_channel_id": 900, "tracking_msg_id": ticket + 1,
                } for ticket, member in ((200, 20), (300, 30), (400, 40))
            },
            "recruitment_trial_reminders": {
                "500": {"applicant_id": 50, "created_at": 1792530000,
                        "resolved_at": 1792640000, "resolved_by": 10,
                        "channel_id": 800, "message_id": 501},
                "600": {"applicant_id": 60, "created_at": 1792530000,
                        "resolved_at": None, "resolved_by": None,
                        "channel_id": 800, "message_id": 601},
            },
            "recruitment_ticket_activity": {},
        }
        for source in SOURCES:
            path = source.resolve(self.paths)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.docs[source.state_name]), encoding="utf-8")

    def query(self, sql, *, levels=frozenset({"recruitment"}), channel_ids=None):
        return run_query(SOURCES, self.paths, levels, sql,
                         channel_ids=channel_ids if channel_ids is not None else {200, 300, 500, 600})

    def test_two_accepted_trials_and_trial_end_records_are_distinct_cohorts(self):
        result, touched = self.query(
            "SELECT t.key AS ticket_id, json_extract(t.value,'$.applicant_id') AS member_id, "
            "json_extract(t.value,'$.tracking_msg_id') AS tracking_message_id "
            "FROM state.recruitment_trials, json_each(doc) t ORDER BY t.key"
        )
        self.assertEqual(result["rows"], [
            {"ticket_id": "200", "member_id": 20, "tracking_message_id": 201},
            {"ticket_id": "300", "member_id": 30, "tracking_message_id": 301},
        ])
        self.assertEqual(touched, {"recruitment"})
        result, _ = self.query(
            "SELECT r.key AS ticket_id, json_extract(r.value,'$.resolved_at') IS NOT NULL "
            "AS end_action_recorded, t.key IS NOT NULL AS active_trial "
            "FROM state.recruitment_trial_reminders reminders, json_each(reminders.doc) r "
            "LEFT JOIN (SELECT t.key FROM state.recruitment_trials trials, "
            "json_each(trials.doc) t) t ON t.key=r.key ORDER BY r.key"
        )
        self.assertEqual(result["rows"], [
            {"ticket_id": "500", "end_action_recorded": 1, "active_trial": 0},
            {"ticket_id": "600", "end_action_recorded": 0, "active_trial": 0},
        ])

    def test_visibility_applies_before_aggregate_and_raw_document_projection(self):
        result, _ = self.query("SELECT COUNT(*) AS n FROM state.recruitment_trials,json_each(doc)")
        self.assertEqual(result["rows"], [{"n": 2}])
        result, _ = self.query('SELECT doc FROM "state"."recruitment_trials"')
        self.assertEqual(set(json.loads(result["rows"][0]["doc"])), {"200", "300"})
        result, _ = self.query("SELECT doc FROM state.recruitment_trials", channel_ids=set())
        self.assertEqual(json.loads(result["rows"][0]["doc"]), {})

    def test_role_and_channel_scope_are_both_required(self):
        result, touched = self.query("SELECT doc FROM state.recruitment_trials", levels=set())
        self.assertEqual(result["required_access"], ["recruitment"])
        self.assertFalse(touched)
        result, touched = run_query(SOURCES, self.paths, {"recruitment"},
                                    "SELECT doc FROM state.recruitment_trials")
        self.assertIn("error", result)
        self.assertFalse(touched)

    def test_missing_and_invalid_state_are_unavailable_not_empty_history(self):
        source = SOURCES[0]
        source.resolve(self.paths).write_text("[]", encoding="utf-8")
        self.assertIn("error", self.query("SELECT doc FROM state.recruitment_trials")[0])
        source.resolve(self.paths).unlink()
        self.assertIn("error", self.query("SELECT doc FROM state.recruitment_trials")[0])

    async def test_context_retains_only_accessible_ticket_sources_and_rechecks(self):
        channels = [SimpleNamespace(id=identifier) for identifier in (200, 300, 400, 500, 600)]
        context = SimpleNamespace(
            bot=SimpleNamespace(paths=self.paths), guild=SimpleNamespace(channels=channels, threads=[]),
            member=SimpleNamespace(id=10), state=AgentTurnState(),
        )
        async def check_access(context):
            if 200 in context.state.source_channels and revoked:
                raise AgentAccessLost("Ticket access lost")
        async def read_and_revoke(function, *args, **kwargs):
            nonlocal revoked
            result = function(*args, **kwargs)
            revoked = True
            return result
        revoked = False
        with (
            patch("elbow_helper.agent.datasets.catalogue.SOURCES", SOURCES),
            patch("elbow_helper.agent.datasets.query.has_reader_access_requirements", return_value=True),
            patch("elbow_helper.agent.datasets.query.can_access_message_channel",
                  AsyncMock(side_effect=lambda _, channel, **kw: channel.id != 400)),
            patch("elbow_helper.agent.datasets.query.require_evidence_access", check_access),
        ):
            result = await query_context(context, "SELECT COUNT(*) AS n FROM state.recruitment_trials,json_each(doc)")
            self.assertEqual(result["rows"], [{"n": 2}])
            self.assertEqual(context.state.source_channels, {200, 300})
            self.assertEqual(context.state.required_access, {"recruitment"})
            context.state.source_channels.clear()
            with patch("elbow_helper.agent.datasets.query.asyncio.to_thread", read_and_revoke):
                with self.assertRaises(AgentAccessLost):
                    await query_context(context, "SELECT doc FROM state.recruitment_trials")

    def test_guide_exposes_workflow_sources_only_at_recruitment_level(self):
        guide = DataGuide(self.paths, SOURCES)
        visible = guide.for_levels({"recruitment"})
        self.assertIn("state.recruitment_trials [recruitment]", visible)
        self.assertIn("trial-list tracking post includes Clans", visible)
        self.assertIn("not a new accept/decline stage", visible)
        self.assertIn("not a complete historical ledger", visible)
        public = guide.for_levels(set()).split("Not available")[0]
        self.assertNotIn("state.recruitment_trials", public)


if __name__ == "__main__":
    unittest.main()

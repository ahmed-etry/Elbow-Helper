from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from elbow_helper.features.examination.queries import (
    ExaminationQueries,
    examination_workflow_status,
)


def _case(channel_id, **changes):
    value = {
        "ticket_channel_id": channel_id,
        "type": "elder_promo",
        "opener_id": 42,
        "exam_required": True,
        "routing_inflight": False,
        "routing_message_id": 900,
        "stage": "initial",
        "responded": False,
        "availability": "private availability",
        "elder_reason": "private answer",
        "pinged_ids": [88],
    }
    value.update(changes)
    return value


class ExaminationQueryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
        self.state = {
            "100": _case(
                100,
                type="clan_promo",
                intake_state="selecting_from",
                exam_required=None,
                routing_message_id=None,
                stage="pending",
            ),
            "200": _case(200, opener_id=None, stage="reminder"),
            "300": _case(300, responded=True),
            "400": _case(400, opener_id=126, elder_reason="hidden answer"),
            "bad": {},
            "500": _case(999),
        }
        self.queries = ExaminationQueries(
            lambda: self.state,
            clock=lambda: self.now,
        )

    def test_registrations_expose_only_candidate_channel_ids(self):
        registrations = self.queries.case_registrations()
        self.assertEqual(
            [row.ticket_channel_id for row in registrations],
            [100, 200, 300, 400, 500],
        )
        self.assertNotIn("private", repr(registrations))
        self.assertNotIn("88", repr(registrations))

    def test_snapshot_projects_only_selected_status_fields(self):
        original = deepcopy(self.state)
        snapshot = self.queries.case_snapshot(
            ticket_channel_ids=(100, 200, 300, 500),
        )

        self.assertEqual(snapshot.selected_entry_count, 4)
        self.assertEqual(snapshot.skipped_invalid_selected_count, 1)
        rows = {row.ticket_channel_id: row for row in snapshot.cases}
        self.assertEqual(rows[100].workflow_status, "awaiting_applicant_intake")
        self.assertEqual(rows[100].exam_requirement, "undetermined")
        self.assertEqual(rows[200].workflow_status, "followup_sent")
        self.assertEqual(rows[200].applicant_status, "unidentified")
        self.assertEqual(rows[300].workflow_status, "response_recorded")
        self.assertNotIn(400, rows)
        self.assertNotIn("availability", repr(snapshot))
        self.assertNotIn("private answer", repr(snapshot))
        self.assertNotIn("hidden answer", repr(snapshot))
        self.assertNotIn("pinged", repr(snapshot))
        self.assertEqual(self.state, original)

    def test_workflow_status_precedence_matches_owner_state(self):
        cases = (
            ({"case_type": "clan_promo", "intake_status": "pending",
              "routing_status": "routed", "followup_stage": "fallback",
              "responded": True}, "awaiting_applicant_intake"),
            ({"case_type": "elder_promo", "intake_status": "not_applicable",
              "routing_status": "in_progress", "followup_stage": "initial",
              "responded": True}, "routing_in_progress"),
            ({"case_type": "elder_promo", "intake_status": "not_applicable",
              "routing_status": "pending", "followup_stage": "initial",
              "responded": True}, "awaiting_routing"),
            ({"case_type": "elder_promo", "intake_status": "not_applicable",
              "routing_status": "routed", "followup_stage": "initial",
              "responded": True}, "response_recorded"),
            ({"case_type": "elder_promo", "intake_status": "not_applicable",
              "routing_status": "routed", "followup_stage": "missing",
              "responded": False}, "missing_application_fields"),
            ({"case_type": "elder_promo", "intake_status": "not_applicable",
              "routing_status": "routed", "followup_stage": "fallback",
              "responded": False}, "fallback_followup_sent"),
            ({"case_type": "elder_promo", "intake_status": "not_applicable",
              "routing_status": "routed", "followup_stage": "reminder",
              "responded": False}, "followup_sent"),
        )
        for arguments, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(examination_workflow_status(**arguments), expected)

    def test_invalid_selected_rows_and_bounds_fail_closed(self):
        for channel_id, change in enumerate((
            {"type": []},
            {"intake_state": []},
            {"stage": []},
            {"responded": "yes"},
            {"routing_inflight": 1},
            {"opener_id": 0},
        ), start=600):
            value = _case(
                channel_id, type="clan_promo", intake_state="complete",
            )
            value.update(change)
            self.state[str(channel_id)] = value
        snapshot = self.queries.case_snapshot(
            ticket_channel_ids=tuple(range(600, 606)),
        )
        self.assertEqual(snapshot.selected_entry_count, 6)
        self.assertEqual(snapshot.skipped_invalid_selected_count, 6)
        self.assertEqual(snapshot.cases, ())

        with self.assertRaises(ValueError):
            self.queries.case_snapshot(ticket_channel_ids=(100, 100))
        with self.assertRaises(ValueError):
            ExaminationQueries(lambda: []).case_registrations()
        with patch(
            "elbow_helper.features.examination.queries.MAX_ACTIVE_EXAMINATION_CASES",
            1,
        ), self.assertRaises(ValueError):
            self.queries.case_registrations()


if __name__ == "__main__":
    unittest.main()

from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from elbow_helper.features.recruitment.queries import RecruitmentQueries


class RecruitmentQueryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
        self.state = {
            "100": {
                "start": "2026-09-16T12:00:00+00:00",
                "days": 2,
                "applicant_id": 42,
                "tracking_msg_id": 999,
                "tracking_channel_id": 888,
                "private_note": "do not expose",
            },
            "200": {
                "start": "2026-09-10T12:00:00+00:00",
                "days": 3,
                "applicant_id": 84,
            },
            "bad": {},
            "300": {"start": "bad", "days": 7, "applicant_id": 126},
            "400": {
                "start": "2026-09-16T12:00:00+00:00",
                "days": "999999999999999999999999999999",
                "applicant_id": 168,
            },
        }
        self.queries = RecruitmentQueries(
            lambda: self.state,
            clock=lambda: self.now,
        )

    def test_registrations_expose_only_candidate_channel_ids(self):
        registrations = self.queries.active_trial_registrations()
        self.assertEqual(
            [row.ticket_channel_id for row in registrations],
            [100, 200, 300, 400],
        )
        self.assertNotIn("private_note", repr(registrations))
        self.assertNotIn("999", repr(registrations))

    def test_snapshot_projects_and_accounts_for_selected_entries_only(self):
        original = deepcopy(self.state)
        snapshot = self.queries.active_trial_snapshot(
            ticket_channel_ids=(100, 300, 400),
        )

        self.assertEqual(snapshot.selected_entry_count, 3)
        self.assertEqual(snapshot.skipped_invalid_selected_count, 2)
        self.assertEqual(len(snapshot.trials), 1)
        trial = snapshot.trials[0]
        self.assertEqual(trial.applicant_member_id, 42)
        self.assertEqual(trial.timing_status, "in_progress")
        self.assertEqual(trial.remaining_seconds, 86_400)
        self.assertIsNone(trial.overdue_seconds)
        self.assertNotIn("tracking", repr(snapshot))
        self.assertNotIn("private_note", repr(snapshot))
        self.assertNotIn(
            84, [row.applicant_member_id for row in snapshot.trials],
        )
        self.assertEqual(self.state, original)

        due = self.queries.active_trial_snapshot(
            ticket_channel_ids=(200,),
        ).trials[0]
        self.assertEqual(due.timing_status, "due")
        self.assertEqual(due.overdue_seconds, 4 * 86_400)

    def test_naive_legacy_start_is_treated_as_utc(self):
        self.state = {
            "100": {
                "start": "2026-09-16T12:00:00",
                "days": 2,
                "applicant_id": 42,
            },
        }
        trial = self.queries.active_trial_snapshot(
            ticket_channel_ids=(100,),
        ).trials[0]
        self.assertEqual(trial.start_at, "2026-09-16T12:00:00+00:00")

    def test_invalid_selection_state_and_bound_fail_closed(self):
        with self.assertRaises(ValueError):
            self.queries.active_trial_snapshot(ticket_channel_ids=(100, 100))
        with self.assertRaises(ValueError):
            RecruitmentQueries(lambda: []).active_trial_registrations()
        with patch(
            "elbow_helper.features.recruitment.queries.MAX_ACTIVE_RECRUITMENT_TRIALS",
            1,
        ), self.assertRaises(ValueError):
            self.queries.active_trial_registrations()


if __name__ == "__main__":
    unittest.main()

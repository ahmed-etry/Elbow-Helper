"""Saved reports keep their exact payloads and validation failures."""

from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
import unittest

from elbow_helper.agent.features.event_stats.report import EventScheduleReport
from elbow_helper.agent.features.examination.report import ExaminationCaseReport, _timestamp as examination_time
from elbow_helper.agent.features.hibernation.report import HibernationReport, _timestamp as hibernation_time
from elbow_helper.agent.features.member_lifecycle.report import MemberLifecycleReport
from elbow_helper.agent.features.recruitment.report import RecruitmentTrialReport, _timestamp as recruitment_time
from elbow_helper.agent.features.support_tickets.report import _timestamp as support_time
from elbow_helper.agent.reports.values import valid_timestamp


@dataclass
class _Snapshot:
    rows: tuple[int, ...]


class ReportValuesTests(unittest.TestCase):
    def test_payload_shapes_and_key_order_stay_distinct(self):
        report = SimpleNamespace(report_id="saved", guild_id=7, source_channel_id=9,
                                 snapshot=_Snapshot((1, 2)))
        for cls in (EventScheduleReport, ExaminationCaseReport, RecruitmentTrialReport,
                    HibernationReport, MemberLifecycleReport):
            with self.subTest(report=cls.__name__):
                expected = {"report_id": "saved", "guild_id": 7}
                if cls in (HibernationReport, MemberLifecycleReport):
                    expected["source_channel_id"] = 9
                expected["snapshot"] = {"rows": (1, 2)}
                payload = cls.storage_payload(report)
                self.assertEqual(payload, expected)
                self.assertEqual(list(payload), list(expected))

    def test_parsers_preserve_offsets_errors_and_exception_causes(self):
        parsers = ((examination_time, "Invalid examination report timestamp"),
                   (recruitment_time, "Invalid recruitment trial timestamp"),
                   (hibernation_time, "Invalid hibernation timestamp"),
                   (support_time, "Invalid support ticket timestamp"))
        for parser, message in parsers:
            for value in (None, 0, "", "bad", "2026-11-07", "2026-11-07T12:30:00"):
                with self.subTest(parser=parser.__module__, value=value):
                    with self.assertRaises(ValueError) as caught:
                        parser(value)
                    self.assertEqual(str(caught.exception), message)
                    self.assertEqual(isinstance(caught.exception.__cause__, ValueError), value == "bad")
            for value in ("2026-11-07T12:30:00Z", "2026-11-07T12:30:00+05:30"):
                self.assertEqual(parser(value), datetime.fromisoformat(value))

    def test_predicate_keeps_length_limit_and_naive_rejection(self):
        for value in (None, True, "", "bad", "2026-11-07", "2026-11-07T12:30:00", "x" * 65):
            with self.subTest(value=value):
                self.assertFalse(valid_timestamp(value))
        for value in ("2026-11-07T12:30:00Z", "2026-11-07T12:30:00+05:30"):
            self.assertTrue(valid_timestamp(value))
        self.assertTrue(valid_timestamp("2026-11-07T12:30:00." + "1" * 38 + "+00:00"))
        self.assertFalse(valid_timestamp("2026-11-07T12:30:00." + "1" * 39 + "+00:00"))

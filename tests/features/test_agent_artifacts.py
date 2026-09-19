from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.features.agent.reports.base import (
    ArtifactCapacityError, retain_report, retain_reports,
)
from elbow_helper.features.agent.reports.roles import RoleAccountReport


class ArtifactTests(unittest.TestCase):
    def test_byte_budget_evicts_oldest_complete_reports(self):
        reports = {}
        with patch("elbow_helper.features.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES", 100):
            for report_id in ("first", "second", "third"):
                retain_report(reports, SimpleNamespace(report_id=report_id, retained_bytes=40))
        self.assertEqual(list(reports), ["second", "third"])

    def test_count_budget_applies_across_report_kinds(self):
        reports = {}
        for index in range(8):
            retain_report(reports, SimpleNamespace(report_id=str(index), retained_bytes=1))
        self.assertEqual(list(reports), ["2", "3", "4", "5", "6", "7"])

    def test_linked_retention_preserves_sources_and_evicts_unrelated_report(self):
        reports = {
            "unrelated": SimpleNamespace(report_id="unrelated", retained_bytes=1),
            **{
                report_id: SimpleNamespace(report_id=report_id, retained_bytes=1)
                for report_id in ("one", "two", "three", "four", "baseline")
            },
        }
        briefing = SimpleNamespace(report_id="briefing", retained_bytes=1)
        retain_report(
            reports, briefing,
            protected_report_ids=("one", "two", "three", "four", "baseline"),
        )
        self.assertEqual(list(reports), [
            "one", "two", "three", "four", "baseline", "briefing",
        ])

    def test_linked_retention_rejects_atomically_when_byte_budget_cannot_fit(self):
        first = SimpleNamespace(report_id="first", retained_bytes=40)
        second = SimpleNamespace(report_id="second", retained_bytes=40)
        reports = {"first": first, "second": second}
        with patch("elbow_helper.features.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES", 100):
            with self.assertRaises(ArtifactCapacityError):
                retain_report(
                    reports,
                    SimpleNamespace(report_id="briefing", retained_bytes=30),
                    protected_report_ids=("first", "second"),
                )
        self.assertEqual(reports, {"first": first, "second": second})

    def test_linked_batch_is_all_or_none_and_keeps_every_addition(self):
        baseline = SimpleNamespace(report_id="baseline", retained_bytes=20)
        unrelated = SimpleNamespace(report_id="unrelated", retained_bytes=20)
        reports = {"unrelated": unrelated, "baseline": baseline}
        additions = tuple(
            SimpleNamespace(report_id=report_id, retained_bytes=20)
            for report_id in ("source-one", "source-two", "briefing")
        )
        with patch("elbow_helper.features.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES", 79):
            with self.assertRaises(ArtifactCapacityError):
                retain_reports(
                    reports, additions,
                    protected_report_ids=(baseline.report_id,),
                )
        self.assertEqual(reports, {
            "unrelated": unrelated, "baseline": baseline,
        })

        with patch("elbow_helper.features.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES", 80):
            retain_reports(
                reports, additions,
                protected_report_ids=(baseline.report_id,),
            )
        self.assertEqual(list(reports), [
            "baseline", "source-one", "source-two", "briefing",
        ])

    def test_oversize_rejection_does_not_destroy_existing_report(self):
        first = SimpleNamespace(report_id="first", retained_bytes=40)
        reports = {"first": first}
        with patch("elbow_helper.features.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES", 100):
            with self.assertRaises(ArtifactCapacityError):
                retain_report(reports, SimpleNamespace(report_id="first", retained_bytes=101))
        self.assertIs(reports["first"], first)

    def test_replacement_counts_only_new_payload(self):
        reports = {"same": SimpleNamespace(report_id="same", retained_bytes=40)}
        replacement = SimpleNamespace(report_id="same", retained_bytes=100)
        with patch("elbow_helper.features.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES", 100):
            retain_report(reports, replacement)
        self.assertEqual(list(reports), ["same"])
        self.assertIs(reports["same"], replacement)

    def test_report_accounting_counts_utf8_payload(self):
        ascii_report = RoleAccountReport("id", "now", ({"name": "x"},), ())
        unicode_report = RoleAccountReport("id", "now", ({"name": "界"},), ())
        self.assertEqual(unicode_report.retained_bytes - ascii_report.retained_bytes, 2)
        self.assertEqual(unicode_report.manifest()["kind"], "role_accounts")

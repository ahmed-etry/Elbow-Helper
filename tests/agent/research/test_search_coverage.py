import unittest

from elbow_helper.agent.research.contracts import validate_research_coverage
from elbow_helper.agent.research.search import _search_coverage
from elbow_helper.infrastructure.discord.message_search import DiscordSearchPage


def _coverage(*, explicit=True, missing=False):
    return _search_coverage(
        DiscordSearchPage((), 0, 10, 20, None, False, False), [],
        {
            "cursor_scope": "synthetic", "max_id": 1000,
            "explicit_ids": (100,) if explicit else (),
            "missing_sources": [{"channel_id": 200}] if missing else [],
            "out_of_scope_results_dropped": 0,
        },
    )


class SearchCoverageTests(unittest.TestCase):
    def test_producer_coverage_with_and_without_disclosable_totals(self):
        for explicit, missing in ((True, False), (False, False), (True, True)):
            with self.subTest(explicit=explicit, missing=missing):
                coverage = _coverage(explicit=explicit, missing=missing)
                validated = validate_research_coverage(coverage, kind="search")
                self.assertEqual(validated, coverage)
                self.assertIsNot(validated, coverage)
                for key in ("total_results_estimate", "returned_indexed_matches"):
                    self.assertEqual(key in validated, explicit and not missing)

    def test_paging_and_checkpoint_fields_are_typed_and_retained(self):
        coverage = {
            **_coverage(), "order": "newest_first", "pages_scanned": 2,
            "returned_indexed_matches": 20, "returned_accessible_matches": 18,
            "out_of_scope_results_dropped": 2,
            "stopped_at_message_budget": True, "stopped_at_request_budget": False,
            "retained_storage_limit_reached": False,
            "retained_message_limit_reached": False,
            "repeated_cursor_detected": False,
        }
        self.assertEqual(validate_research_coverage(coverage, kind="search"), coverage)
        for key in (
            "deep_historical_indexing", "offset_limit_reached",
            "reached_current_indexed_end",
            "total_may_change_while_messages_are_created_or_deleted",
            "stopped_at_message_budget", "stopped_at_request_budget",
            "retained_storage_limit_reached", "retained_message_limit_reached",
            "repeated_cursor_detected",
        ):
            for invalid in (None, 0, "false"):
                with self.subTest(key=key, invalid=invalid):
                    with self.assertRaises(ValueError):
                        validate_research_coverage({**coverage, key: invalid}, kind="search")

    def test_rejects_invalid_counts_order_and_unknown_fields(self):
        invalid_values = {
            "total_results_estimate": (None, True, -1, 1.5, "20"),
            "out_of_scope_results_dropped": (None, True, -1, 1.5, "2"),
            "pages_scanned": (None, True, 0, 9, 1.5, "2"),
            "order": (None, True, 1, "oldest_first"),
            "offset": (True, -1, "0"),
            "requested_page_size": (True, 0, 11, "10"),
            "returned_indexed_matches": (True, -1, 11, "1"),
            "returned_accessible_matches": (True, -1, 1, "1"),
            "next_offset": (True, -1, 9976, "10"),
            "snapshot_before_message_id": (True, 0, "1000"),
            "next_cursor": (True, 1, "x" * 65),
            "unexpected_field": (False,),
        }
        for key, values in invalid_values.items():
            for invalid in values:
                with self.subTest(key=key, invalid=invalid):
                    with self.assertRaises(ValueError):
                        validate_research_coverage({**_coverage(), key: invalid}, kind="search")

    def test_present_total_accepts_zero_and_positive_integers(self):
        for total in (0, 20):
            coverage = {**_coverage(), "total_results_estimate": total}
            self.assertEqual(validate_research_coverage(coverage, kind="search"), coverage)

    def test_aggregate_indexed_count_is_bounded_by_pages(self):
        coverage = {**_coverage(), "pages_scanned": 8, "returned_indexed_matches": 80}
        self.assertEqual(validate_research_coverage(coverage, kind="search"), coverage)
        with self.assertRaises(ValueError):
            validate_research_coverage({**coverage, "returned_indexed_matches": 81}, kind="search")
        coverage.pop("pages_scanned")
        with self.assertRaises(ValueError):
            validate_research_coverage(coverage, kind="search")

    def test_required_fields_still_required_without_total(self):
        for key in (
            "next_cursor", "deep_historical_indexing",
            "offset_limit_reached", "reached_current_indexed_end",
        ):
            with self.subTest(key=key):
                coverage = _coverage(explicit=False)
                coverage.pop(key)
                with self.assertRaises(ValueError):
                    validate_research_coverage(coverage, kind="search")

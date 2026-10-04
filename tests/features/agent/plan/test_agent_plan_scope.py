"""Retained results preserve their original checked scope."""

import json
from types import SimpleNamespace
import unittest

from elbow_helper.features.agent.plan.scope import ScopeLedger, resource_ids
from elbow_helper.features.agent.engine.capability_contract import CapabilityContract

from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.engine.capability_contract import contract_catalogue
from elbow_helper.features.agent.reports.tools import saved_report_contracts
from features.agent.engine.helpers import patch_contracts

REGISTRY = build_agent_tools()
CONTRACTS = contract_catalogue(REGISTRY)
SAVED_REPORT_CONTRACTS = saved_report_contracts(REGISTRY)


class ScopeLedgerTests(unittest.TestCase):
    def context(self, evidence=()):
        turn = SimpleNamespace(record=SimpleNamespace(evidence=evidence))
        return SimpleNamespace(state=SimpleNamespace(authorized_history=(turn,)), history=())

    def test_every_retained_field_is_found_inside_nested_results(self):
        for contract in CONTRACTS.values():
            for field in contract.retained_fields:
                with self.subTest(field=field):
                    result = {"items": [{field: "synthetic-id"}, {field: ["other-id"]}]}
                    self.assertEqual(resource_ids(result, registry=REGISTRY), {"synthetic-id", "other-id"})

    def test_retained_read_cannot_replace_its_original_scope(self):
        origin = CapabilityContract((("source_id", "synthetic_source"),), ("period_key",))
        reader = CapabilityContract((("resource_id", "synthetic_resource"),), (), retained_fields=("resource_id",))
        first = {"tool": "read_source", "arguments": {"source_id": 101, "period_key": "selected"},
                 "result": json.dumps({"resource_id": "synthetic-id"})}
        second = {"tool": "read_retained", "arguments": {"resource_id": "synthetic-id"},
                  "result": json.dumps({"resource_id": "synthetic-id"})}
        with patch_contracts(REGISTRY, {"read_source": origin, "read_retained": reader}):
            ledger = ScopeLedger(self.context(tuple(map(json.dumps, (first, second)))), registry=REGISTRY)
            self.assertEqual(ledger.reports["synthetic-id"], ("read_source", first["arguments"]))
            periods = (("key", "selected", "period_key"),)
            named = {"synthetic_source": {"101"}}
            self.assertEqual(ledger.check(["synthetic-id"], periods, named), "")
            self.assertTrue(ledger.check(["synthetic-id"], (("key", "other", "period_key"),), named))
            other = {"synthetic_source": {"202"}}
            self.assertTrue(ledger.check(["synthetic-id"], periods, other))

    def test_unknown_retained_scope_is_refused_for_bounded_requests(self):
        ledger = ScopeLedger(self.context(), registry=REGISTRY)
        self.assertTrue(ledger.check(["synthetic-id"], (("key", "selected", "period_key"),), {}))
        self.assertTrue(ledger.check(["synthetic-id"], (), {"synthetic_source": {"101"}}))

    def test_typed_reference_provenance_preserves_named_and_period_limits(self):
        origin = CapabilityContract((("source_id", "synthetic_source"),), ("period_key",))
        with patch_contracts(REGISTRY, {"read_source": origin}):
            ledger = ScopeLedger(self.context(), registry=REGISTRY)
            ledger.remember("synthetic-id", "read_source", {"source_id": 101, "period_key": "selected"})
            periods = (("key", "selected", "period_key"),)
            self.assertEqual(ledger.check(["synthetic-id"], periods, {}), "")
            self.assertEqual(ledger.check(["synthetic-id"], periods, {"synthetic_source": {"101"}}), "")
            self.assertTrue(ledger.check(["synthetic-id"], periods, {"synthetic_source": {"202"}}))
            self.assertTrue(ledger.check(["synthetic-id"], (("key", "other", "period_key"),), {}))

    def test_original_arguments_survive_history_without_declarations(self):
        from elbow_helper.features.agent.plan.checker import parse_periods
        origin = CapabilityContract((("channel_id", "discord_channel"),), ("after", "before"),
                                    time_window=("after", "before", "iso_utc"))
        reader = CapabilityContract((("report_id", "synthetic_report"),), (), retained_fields=("report_id",))
        arguments = {"channel_id": 101, "after": "2026-01-02", "before": "2026-01-03"}
        evidence = json.dumps({"tool": "read_source", "arguments": arguments,
                               "result": json.dumps({"report_id": "synthetic-report"})})
        with patch_contracts(REGISTRY, {"read_source": origin, "read_retained": reader}):
            fresh = ScopeLedger(self.context(), registry=REGISTRY)
            fresh.remember("synthetic-report", "read_source", arguments)
            restored = ScopeLedger(self.context((evidence,)), registry=REGISTRY)
            for ledger in (fresh, restored):
                self.assertEqual(ledger.check(["synthetic-report"], (), {}), "")
                self.assertEqual(ledger.check(["synthetic-report"], (), {"discord_channel": {"101"}}), "")
                self.assertTrue(ledger.check(["synthetic-report"], (), {"discord_channel": {"202"}}))
                inside = parse_periods([{"kind": "utc_range", "start": "2026-01-01", "end": "2026-01-04"}])
                outside = parse_periods([{"kind": "utc_range", "start": "2026-01-03", "end": "2026-01-04"}])
                self.assertEqual(ledger.check(["synthetic-report"], inside, {}), "")
                self.assertEqual(ledger.check(["synthetic-report"], outside, {}), "The time window is outside the selected periods.")

    def test_malformed_saved_evidence_is_ignored(self):
        ledger = ScopeLedger(self.context(("not-json", "null", "[]", json.dumps({"result": "null"}))), registry=REGISTRY)
        self.assertEqual(ledger.reports, {})

    def test_unscoped_reuse_needs_no_declarations_or_temporal_metadata(self):
        ledger = ScopeLedger(self.context(), registry=REGISTRY)
        ledger.remember("synthetic-id", "synthetic_old_capability", {"source_id": 101})
        self.assertEqual(ledger.check(["synthetic-id"], (), {}), "")

    def test_report_without_a_window_cannot_satisfy_a_stated_range(self):
        from elbow_helper.features.agent.plan.checker import parse_periods
        origin = CapabilityContract((("source_id", "synthetic_source"),), ())
        with patch_contracts(REGISTRY, {"read_source": origin}):
            ledger = ScopeLedger(self.context(), registry=REGISTRY)
            ledger.remember("synthetic-id", "read_source", {"source_id": 101})
            periods = parse_periods([{"kind": "utc_range", "start": "2026-01-01", "end": "2026-01-02"}])
            self.assertEqual(ledger.check(["synthetic-id"], periods, {}),
                             "The retained report has no time window for the selected period.")

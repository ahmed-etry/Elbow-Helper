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
            self.assertEqual(ledger.check(["synthetic-id"], periods, named, named), "")
            self.assertTrue(ledger.check(["synthetic-id"], (("key", "other", "period_key"),), named, named))
            other = {"synthetic_source": {"202"}}
            self.assertTrue(ledger.check(["synthetic-id"], periods, other, other))

    def test_unknown_retained_scope_is_refused_for_bounded_requests(self):
        ledger = ScopeLedger(self.context(), registry=REGISTRY)
        self.assertTrue(ledger.check(["synthetic-id"], (("key", "selected", "period_key"),), {}, {}))
        self.assertTrue(ledger.check(["synthetic-id"], (), {"synthetic_source": {"101"}}, {}))

    def test_malformed_saved_evidence_is_ignored(self):
        ledger = ScopeLedger(self.context(("not-json", "null", "[]", json.dumps({"result": "null"}))), registry=REGISTRY)
        self.assertEqual(ledger.reports, {})

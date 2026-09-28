"""Registry-wide structured scope and provenance checks."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from elbow_helper.features.agent import semantic
from elbow_helper.features.agent.semantic import (
    SemanticBindError, require_source_provenance, validate_contract_catalogue,
)
from elbow_helper.features.agent.tools import build_agent_tools


class AgentSemanticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = build_agent_tools()

    def test_every_registered_capability_has_one_structured_contract(self):
        validate_contract_catalogue(self.registry)
        self.assertEqual(set(self.registry), set(semantic.CONTRACTS))
        missing = dict(self.registry)
        missing.pop(next(iter(missing)))
        with self.assertRaisesRegex(ValueError, "missing tools"):
            validate_contract_catalogue(missing)
        with self.assertRaisesRegex(ValueError, "lack semantic contracts"):
            validate_contract_catalogue({**self.registry, "unclassified": next(iter(self.registry.values()))})

    def test_invalid_access_and_field_descriptors_fail_across_registry(self):
        for name, contract in semantic.CONTRACTS.items():
            invalid = [replace(contract, required_access=frozenset({"unknown"})),
                       replace(contract, latest_fields=("unknown",))]
            if contract.entity_fields:
                invalid.append(replace(
                    contract, entity_fields=contract.entity_fields + contract.entity_fields[:1],
                ))
            if contract.time_fields:
                invalid.append(replace(contract, time_fields=contract.time_fields + contract.time_fields[:1]))
            for changed in invalid:
                with self.subTest(capability=name, contract=changed):
                    with patch.object(semantic, "CONTRACTS", {**semantic.CONTRACTS, name: changed}):
                        with self.assertRaises(ValueError):
                            validate_contract_catalogue(self.registry)

    def test_scope_variants_and_time_windows_match_every_schema(self):
        checked = 0
        for name, contract in semantic.CONTRACTS.items():
            if contract.scope_variants:
                changed = replace(contract, scope_variants=contract.scope_variants + contract.scope_variants[:1])
                with self.subTest(capability=name, kind="scope"):
                    with patch.object(semantic, "CONTRACTS", {**semantic.CONTRACTS, name: changed}):
                        with self.assertRaises(ValueError):
                            validate_contract_catalogue(self.registry)
                checked += 1
            if contract.time_window:
                lower, upper, encoding = contract.time_window
                other_encoding = "unix_seconds" if encoding == "iso_utc" else "iso_utc"
                changed = replace(contract, time_window=(lower, upper, other_encoding))
                with self.subTest(capability=name, kind="time"):
                    with patch.object(semantic, "CONTRACTS", {**semantic.CONTRACTS, name: changed}):
                        with self.assertRaises(ValueError):
                            validate_contract_catalogue(self.registry)
                checked += 1
        self.assertGreater(checked, 0)

    def test_every_source_bearing_result_rejects_unbound_channels(self):
        checked = 0
        for name, contract in semantic.CONTRACTS.items():
            if contract.source_scope not in {
                "channel_messages", "channel_status", "retained_channel_evidence",
                "request_attachment", "retained_attachment",
            }:
                continue
            if not contract.result_channel_fields and not contract.result_channel_lists:
                continue
            scope = {
                "source_scope": contract.source_scope,
                "channel_fields": contract.channel_fields,
                "result_channel_fields": contract.result_channel_fields,
                "result_channel_lists": contract.result_channel_lists,
                "result_sources_within_query": contract.result_sources_within_query,
                "bound_source_channels": (),
            }
            arguments = {field: 101 for field in contract.channel_fields}
            payload = {field: 101 for field in contract.result_channel_fields}
            payload.update({collection: [{field: 101}]
                            for collection, field in contract.result_channel_lists})
            with self.subTest(capability=name):
                require_source_provenance(scope, arguments, {101}, payload)
                bad = dict(payload)
                if contract.result_channel_fields:
                    bad[contract.result_channel_fields[0]] = 202
                else:
                    collection, field = contract.result_channel_lists[0]
                    bad[collection] = [{field: 202}]
                with self.assertRaises(SemanticBindError):
                    require_source_provenance(scope, arguments, {101}, bad)
                checked += 1
        self.assertGreater(checked, 0)

    def test_selected_channel_results_cannot_swap_to_another_accessible_source(self):
        checked = 0
        for name, contract in semantic.CONTRACTS.items():
            if not contract.result_sources_within_query or not contract.channel_fields:
                continue
            if not contract.result_channel_fields and not contract.result_channel_lists:
                continue
            scope = {
                "source_scope": contract.source_scope,
                "channel_fields": contract.channel_fields,
                "result_channel_fields": contract.result_channel_fields,
                "result_channel_lists": contract.result_channel_lists,
                "result_sources_within_query": True,
            }
            arguments = {field: 101 for field in contract.channel_fields}
            payload = {field: 202 for field in contract.result_channel_fields}
            payload.update({collection: [{field: 202}]
                            for collection, field in contract.result_channel_lists})
            with self.subTest(capability=name):
                with self.assertRaises(SemanticBindError):
                    require_source_provenance(scope, arguments, {101, 202}, payload)
                checked += 1
        self.assertGreater(checked, 0)

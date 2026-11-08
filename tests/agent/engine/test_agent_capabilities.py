"""Registry-wide structured scope and provenance checks."""

from __future__ import annotations

import unittest

from elbow_helper.agent.engine.capability_contract import CapabilityBindError
from elbow_helper.agent.engine.capability_contract import require_source_provenance
from elbow_helper.agent.engine.registry import build_agent_tools

from elbow_helper.agent.engine.capability_contract import contract_catalogue

REGISTRY = build_agent_tools()
CONTRACTS = contract_catalogue(REGISTRY)


class AgentCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = build_agent_tools()


    def test_every_source_bearing_result_rejects_unbound_channels(self):
        checked = 0
        for name, contract in CONTRACTS.items():
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
                with self.assertRaises(CapabilityBindError):
                    require_source_provenance(scope, arguments, {101}, bad)
                checked += 1
        self.assertGreater(checked, 0)

    def test_selected_channel_results_cannot_swap_to_another_accessible_source(self):
        checked = 0
        for name, contract in CONTRACTS.items():
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
                with self.assertRaises(CapabilityBindError):
                    require_source_provenance(scope, arguments, {101, 202}, payload)
                checked += 1
        self.assertGreater(checked, 0)

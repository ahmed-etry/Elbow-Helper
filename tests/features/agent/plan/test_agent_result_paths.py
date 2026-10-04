"""Result references are checked before execution, using synthetic data."""

from dataclasses import replace
import unittest

from elbow_helper.features.agent.engine.capability_contract import CapabilityContract, validate_contract_catalogue
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.features.agent.plan.checker import check_plan, source_check
from elbow_helper.features.agent.plan.executor import execute_plan, resolve_arguments
from elbow_helper.features.agent.plan.format import capability_list
from elbow_helper.infrastructure.ai import AgentToolDefinition


class ResultPathTests(unittest.TestCase):
    def setUp(self):
        self.contract = CapabilityContract((), (), result_entity_keys=(("roles[].role_id", "discord_role"),), result_paths=(
            ("roles", "N", "role_id"), ("groups", "N", "members", "N", "member_id"),
        ))
        self.registry = {
            "lookup": RegisteredAgentTool(AgentToolDefinition("lookup", "Synthetic lookup.", {
                "type": "object", "properties": {},
            }), None, contract=self.contract),
            "consume": RegisteredAgentTool(AgentToolDefinition("consume", "Synthetic consumer.", {
                "type": "object", "properties": {
                    "values": {"type": "array", "items": {"type": "object", "properties": {
                        "value": {"type": "integer"},
                    }}},
                },
            }), None),
        }

    def plan(self, path):
        return {"goal": "Use a synthetic result", "effort": "low", "output": "text",
                "periods": [], "entities": [], "steps": [
                    {"id": "source", "capability": "lookup", "arguments": {},
                     "reason": "Resolve a synthetic role", "depends_on": []},
                    {"id": "consumer", "capability": "consume", "arguments": {
                        "values": [{"value": {"step": "source", "path": path}}],
                    }, "reason": "Use the result", "depends_on": ["source"]},
                ]}

    def test_declared_paths_accept_integer_indexes_in_nested_arguments(self):
        for path in (["roles", 0, "role_id"], ["roles", 7, "role_id"],
                     ["groups", 1, "members", 2, "member_id"]):
            with self.subTest(path=path):
                checked = check_plan(self.plan(path), self.registry)
                self.assertTrue(checked.ok, checked.error)

    def test_star_passes_one_field_from_every_listed_item(self):
        self.assertTrue(check_plan(self.plan(["roles", "*", "role_id"]), self.registry).ok)
        self.assertTrue(check_plan(self.plan(["groups", "*", "members", "*", "member_id"]),
                                    self.registry).ok)
        resolved = resolve_arguments(
            {"ids": {"step": "source", "path": ["roles", "*", "role_id"]}},
            {"source": {"roles": [{"role_id": 3}, {"role_id": 5}]}},
        )
        self.assertEqual(resolved, {"ids": [3, 5]})
        merged = resolve_arguments(
            {"ids": [{"step": "a", "path": ["roles", "*", "role_id"]},
                     {"step": "b", "path": ["roles", "*", "role_id"]}]},
            {"a": {"roles": [{"role_id": 3}]}, "b": {"roles": [{"role_id": 5}, {"role_id": 7}]}},
        )
        self.assertEqual(merged, {"ids": [3, 5, 7]})

    def test_reference_outside_depends_on_names_the_step(self):
        plan = self.plan(["roles", 0, "role_id"])
        plan["steps"][1]["depends_on"] = []
        checked = check_plan(plan, self.registry)
        self.assertFalse(checked.ok)
        self.assertIn("uses results of step 'source'", checked.error)
        self.assertIn("depends_on", checked.error)

    def test_star_only_feeds_list_arguments(self):
        contract = CapabilityContract((("member_ids", "discord_member_set"), ("member_id", "discord_member")), (),
                                      result_paths=(("members", "N", "member_id"),),
                                      result_path_kinds=((("members", "N", "member_id"), "discord_member"),))
        registry = {**self.registry, "lookup": replace(self.registry["lookup"], contract=contract),
                    "consume": RegisteredAgentTool(AgentToolDefinition("consume", "Synthetic consumer.", {
                        "type": "object", "properties": {
                            "member_ids": {"type": "array", "items": {"type": "integer"}},
                            "member_id": {"type": "integer"},
                        },
                    }), None, contract=contract)}
        for field, ok in (("member_ids", True), ("member_id", False)):
            with self.subTest(field=field):
                plan = self.plan(["members", "*", "member_id"])
                plan["steps"][1]["arguments"] = {field: {"step": "source", "path": ["members", "*", "member_id"]}}
                self.assertEqual(check_plan(plan, registry).ok, ok)

    def test_bad_paths_name_source_bad_path_and_valid_paths(self):
        for path in (["roles", 0, "id"], ["roles", "0", "role_id"],
                     ["roles", "N", "role_id"], ["roles", True, "role_id"],
                     ["roles", -1, "role_id"], ["roles"],
                     ["groups", 0, "members", "1", "member_id"]):
            with self.subTest(path=path):
                checked = check_plan(self.plan(path), self.registry)
                self.assertFalse(checked.ok)
                if len(path) == 1 or type(path[1]) is not bool and path[1] != -1:
                    self.assertIn("source", checked.error)
                    self.assertIn(repr(path), checked.error)
                    self.assertIn("roles/N/role_id", checked.error)
                self.assertEqual(checked.step_id, "consumer")

    def test_missing_declaration_forbids_references(self):
        for contract in (None, CapabilityContract((), ())):
            with self.subTest(contract=contract):
                self.registry["lookup"] = replace(self.registry["lookup"], contract=contract)
                checked = check_plan(self.plan(["roles", 0, "role_id"]), self.registry)
                self.assertFalse(checked.ok)
                self.assertIn("Valid paths: none", checked.error)

    def test_invalid_optional_entity_references_are_logged_and_ignored(self):
        plan = self.plan(["roles", 0, "role_id"])
        plan["entities"] = [{"kind": "discord_role", "value": {
            "step": "source", "path": ["roles", 0, "id"],
        }}]
        with self.assertLogs("elbow_helper.features.agent.plan.checker", level="INFO") as logs:
            checked = check_plan(plan, self.registry)
        self.assertTrue(checked.ok, checked.error)
        self.assertIn("Ignoring optional agent entity 1", logs.output[0])
        plan["entities"][0]["value"]["path"][-1] = "role_id"
        with self.assertNoLogs("elbow_helper.features.agent.plan.checker", level="INFO"):
            self.assertTrue(check_plan(plan, self.registry).ok)

    def test_catalogue_advertises_generic_indexes_and_nested_paths(self):
        catalogue = capability_list(self.registry)
        self.assertIn("results roles/N/role_id:discord_role,groups/N/members/N/member_id", catalogue)
        self.assertNotIn("roles/0/role_id", catalogue)

    def test_explicit_path_kinds_normalize_and_appear_in_the_catalogue(self):
        path = ("groups", "N", "members", "N", "member_id")
        contract = replace(self.contract, result_path_kinds=((path, "discord_member_set"),))
        self.registry["lookup"] = replace(self.registry["lookup"], contract=contract)
        validate_contract_catalogue({"lookup": self.registry["lookup"]})
        self.assertEqual(contract.result_path_kind(["groups", 1, "members", 2, "member_id"]), "discord_member")
        self.assertIsNone(contract.result_path_kind(["groups", "1", "members", 2, "member_id"]))
        self.assertIn("groups/N/members/N/member_id:discord_member", capability_list(self.registry))
        self.assertIn("roles/N/role_id:discord_role", capability_list(self.registry))

    def test_invalid_and_conflicting_result_path_kinds_are_refused(self):
        role = ("roles", "N", "role_id")
        for annotations in (((role, ""),), ((("unknown",), "discord_role"),),
                            ((role, "discord_role"), (role, "discord_role")),
                            ((role, "discord_member"),)):
            with self.subTest(annotations=annotations):
                tool = replace(self.registry["lookup"], contract=replace(self.contract, result_path_kinds=annotations))
                with self.assertRaisesRegex(ValueError, "result path kind"):
                    validate_contract_catalogue({"lookup": tool})

    def test_literals_need_no_declaration_but_named_sources_still_apply(self):
        contract = CapabilityContract((("role_id", "discord_role"), ("other_id", "discord_role")), ())
        arguments = {"role_id": 101, "other_id": 202}
        self.assertEqual(source_check(contract, arguments, {})[0], "")
        issue, offered = source_check(contract, arguments, {"discord_role": {"101"}})
        self.assertIn("The request named other sources", issue)
        self.assertEqual(offered, ("202",))

    def test_invalid_optional_entities_log_once_without_mutating_the_plan(self):
        for entity, expected in (
            ({"kind": "discord_role"}, "expected exactly kind and value"),
            ({"kind": "", "value": 101}, "kind must be a non-empty string"),
            ({"kind": [], "value": 101}, "kind must be a non-empty string"),
            ({"kind": "synthetic_unknown", "value": 101}, "unknown kind"),
            ({"kind": "synthetic_unknown_set", "value": {"step": "missing", "path": []}}, "unknown kind"),
            ({"kind": "discord_role", "value": [101]}, "only set kinds"),
            ({"kind": "discord_role_set", "value": []}, "only set kinds"),
            ({"kind": "discord_role_set", "value": [101, True]}, "value must be an ID"),
            ({"kind": "discord_role", "value": ""}, "value must be an ID"),
            ({"kind": "discord_role", "value": None}, "value must be an ID"),
            ({"kind": "discord_role", "value": {"step": "missing", "path": ["roles", 0, "role_id"]}},
             "planned result reference"),
            ({"kind": "discord_role_set", "value": [{"step": "source", "path": []}]},
             "planned result reference"),
            (None, "expected exactly kind and value"),
        ):
            with self.subTest(entity=entity):
                plan = self.plan(["roles", 0, "role_id"])
                plan["entities"] = [entity]
                with self.assertLogs("elbow_helper.features.agent.plan.checker", level="INFO") as logs:
                    checked = check_plan(plan, self.registry)
                self.assertTrue(checked.ok, checked.error)
                self.assertEqual(len(logs.output), 1)
                self.assertIn(expected, logs.output[0])
                self.assertEqual(plan["entities"], [entity])

    def test_invalid_entities_do_not_relax_enforced_step_references(self):
        plan = self.plan(["roles", 0, "id"])
        plan["entities"] = [{"kind": "synthetic_unknown", "value": None}]
        checked = check_plan(plan, self.registry)
        self.assertFalse(checked.ok)
        self.assertIn("does not expose result path", checked.error)

    def test_entity_container_and_count_remain_bounded(self):
        for entities in (None, {}, [{"kind": "discord_role", "value": 101}] * 33):
            with self.subTest(entities=entities):
                plan = self.plan(["roles", 0, "role_id"])
                plan["entities"] = entities
                checked = check_plan(plan, self.registry)
                self.assertFalse(checked.ok)
                self.assertEqual(checked.error, "List at most 32 entities.")

    def test_entity_paths_merge_with_explicit_paths_without_duplicates(self):
        contract = replace(self.contract, result_entity_keys=(
            ("roles[].role_id", "discord_role"),
            ("groups[].members[].member_id", "discord_member"),
            ("summary.owner.member_id", "discord_member"),
        ))
        expected = (*self.contract.result_paths, ("summary", "owner", "member_id"))
        self.assertEqual(contract.referenceable_result_paths, expected)
        self.assertEqual(contract.catalogue_entry()["result_paths"], expected)
        self.registry["lookup"] = replace(self.registry["lookup"], contract=contract)
        validate_contract_catalogue({"lookup": self.registry["lookup"]})
        self.assertTrue(check_plan(self.plan(["summary", "owner", "member_id"]), self.registry).ok)
        text = capability_list(self.registry)
        self.assertIn("summary/owner/member_id", text)
        self.assertEqual(text.count("roles/N/role_id"), 1)
        self.assertIn(" | results ", text)

    def test_merged_result_path_validation_rejects_invalid_entity_paths(self):
        for field in ("roles[]..role_id", ".member_id", "[].member_id",
                      ".".join(["nested"] * 9)):
            with self.subTest(field=field):
                contract = replace(self.contract, result_entity_keys=((field, "discord_member"),))
                tool = replace(self.registry["lookup"], contract=contract)
                with self.assertRaisesRegex(ValueError, "Invalid merged result path"):
                    validate_contract_catalogue({"lookup": tool})


class UnresolvedReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_warning_names_step_and_path_without_result_values(self):
        for source in ({"roles": []}, {"roles": [{}]}, {"roles": None}, {}):
            calls = []
            async def run(step, arguments, earlier):
                calls.append(step["id"])
                return {**source, "secret": "synthetic-private-value"}
            plan = {"steps": [
                {"id": "source", "arguments": {}, "depends_on": []},
                {"id": "consumer", "arguments": {"nested": [{"step": "source", "path": ["roles", 0, "role_id"]}]},
                 "depends_on": ["source"]},
            ]}
            with self.subTest(source=source), self.assertLogs(
                "elbow_helper.features.agent.plan.executor", level="WARNING",
            ) as logs:
                results = await execute_plan(plan, run)
            self.assertEqual(results["consumer"], {"error": "A required earlier result is unavailable."})
            self.assertEqual(calls, ["source"])
            self.assertEqual(len(logs.records), 1)
            self.assertIn("consumer", logs.output[0])
            self.assertIn("source", logs.output[0])
            self.assertIn("['roles', 0, 'role_id']", logs.output[0])
            self.assertNotIn("synthetic-private-value", logs.output[0])

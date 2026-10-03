"""Result references are checked before execution, using synthetic data."""

from dataclasses import replace
import unittest

from elbow_helper.features.agent.engine.capability_contract import CapabilityContract, validate_contract_catalogue
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.features.agent.plan.checker import check_plan, source_check
from elbow_helper.features.agent.plan.executor import execute_plan
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

    def test_entity_references_also_require_declared_paths(self):
        plan = self.plan(["roles", 0, "role_id"])
        plan["entities"] = [{"kind": "discord_role", "value": {
            "step": "source", "path": ["roles", 0, "id"],
        }}]
        checked = check_plan(plan, self.registry)
        self.assertFalse(checked.ok)
        self.assertIn("Entity 1", checked.error)
        plan["entities"][0]["value"]["path"][-1] = "role_id"
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

    def test_typed_reference_declarations_apply_only_to_the_bound_field(self):
        contract = CapabilityContract((("role_id", "discord_role"), ("other_id", "discord_role")), ())
        issue, _ = source_check(contract, {"role_id": 101, "other_id": 101}, {}, {}, {},
                                declared_references={"role_id": {"101"}})
        self.assertEqual(issue, "Declare this entity in the plan.")
        issue, _ = source_check(contract, {"role_id": 101}, {}, {}, {},
                                declared_references={"role_id": {"101"}})
        self.assertEqual(issue, "")

    def test_entity_validation_identifies_the_entity_and_allowed_values(self):
        for entity, expected in (
            ({"kind": "discord_role"}, "Entity 1 must be an object"),
            ({"kind": "", "value": 101}, "Entity 1 kind must be a non-empty string"),
            ({"kind": "discord_role", "value": [101]}, "use discord_role_set"),
            ({"kind": "discord_role_set", "value": []}, "needs a non-empty list"),
            ({"kind": "discord_role_set", "value": [101, True]}, "Entity 1 value 2 must be an ID"),
            ({"kind": "discord_role", "value": ""}, "Entity 1 value 1 must be an ID"),
            ({"kind": "discord_role_set", "value": [{"step": "source", "path": []}]},
             "reference with step and path"),
        ):
            with self.subTest(entity=entity):
                plan = self.plan(["roles", 0, "role_id"])
                plan["entities"] = [entity]
                checked = check_plan(plan, self.registry)
                self.assertFalse(checked.ok)
                self.assertIn(expected, checked.error)

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

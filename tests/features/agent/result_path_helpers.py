"""Check feature-owned result declarations against actual synthetic test results."""

from elbow_helper.features.agent.engine.registry import build_agent_tool_groups


def assert_result_paths(test, capability, result):
    tool = next(tool for group in build_agent_tool_groups().values() for tool in group
                if tool.definition.name == capability)
    test.assertTrue(tool.contract.referenceable_result_paths, capability)
    for path in tool.contract.referenceable_result_paths:
        with test.subTest(capability=capability, result_path=path):
            def visit(value, parts):
                if not parts:
                    return
                part, *rest = parts
                if part == "N":
                    test.assertIsInstance(value, (list, tuple))
                    # Empty result pages have no rows whose fields can be checked.
                    for row in value:
                        visit(row, rest)
                else:
                    test.assertIn(part, value)
                    visit(value[part], rest)
            visit(result, path)

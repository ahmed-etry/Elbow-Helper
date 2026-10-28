"""Keep the reviewed recurring-edit migration and direct-call exceptions visible."""

import ast
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class PublishingCallerTests(unittest.TestCase):
    def test_every_edit_caller_has_a_reviewed_queue_or_direct_decision(self):
        inventory = json.loads((ROOT / "tests/fixtures/publishing_callers.json").read_text(encoding="utf-8"))
        queued, direct = {}, {}

        def visit(path, node, trail=()):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                trail += (node.name,)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"edit", "edit_message", "move", "edit_channel_positions",
                                      "edit_channel", "reorder"}:
                    key = f'{path.relative_to(ROOT).as_posix()}:{".".join(trail)}'
                    receiver = ast.unparse(node.func.value)
                    if receiver.endswith(".publishing"):
                        queued.setdefault(key, set()).add(node.func.attr)
                    else:
                        direct.setdefault(key, set()).add(receiver + "." + node.func.attr)
            for child in ast.iter_child_nodes(node):
                visit(path, child, trail)

        for path in (ROOT / "elbow_helper").rglob("*.py"):
            if path != ROOT / "elbow_helper/discord/publishing.py":
                visit(path, ast.parse(path.read_text(encoding="utf-8-sig")))

        self.assertEqual({key: sorted(value) for key, value in queued.items()}, inventory["queued"])
        self.assertEqual({key: sorted(value) for key, value in direct.items()},
                         {key: value["operations"] for key, value in inventory["direct"].items()})
        self.assertTrue(all(value["reason"] for value in inventory["direct"].values()))


if __name__ == "__main__":
    unittest.main()

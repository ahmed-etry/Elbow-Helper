"""Recovery handlers belong to the action paths their modules prepare."""

import ast
import inspect
import unittest

from elbow_helper.features.agent.actions.undo import merge_undo_handlers
from elbow_helper.features.agent.engine.registry import build_undo_handlers


def possible_paths(expression, assignments, visited=frozenset()):
    paths = set()
    for node in ast.walk(expression):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            paths.add(node.value)
        elif isinstance(node, ast.Name) and node.id not in visited:
            for value in assignments.get(node.id, ()):
                paths.update(possible_paths(value, assignments, visited | {node.id}))
    return paths


def prepared_paths(module):
    tree = ast.parse(inspect.getsource(module))
    paths = set()
    for function in tree.body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        assignments = {}
        for node in ast.walk(function):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assignments.setdefault(target.id, []).append(node.value)
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id == "PreparedAction" or (
                node.func.id == "CommandAdapter"
                and any(keyword.arg == "prepare" for keyword in node.keywords)
            ):
                paths.update(possible_paths(node.args[0], assignments))
    return paths


class UndoRegistryTests(unittest.TestCase):
    def test_every_undo_key_is_prepared_by_its_owning_module(self):
        handlers = build_undo_handlers()
        self.assertTrue(handlers)
        for path, handler in handlers.items():
            module = inspect.getmodule(handler)
            with self.subTest(path=path, module=module.__name__):
                self.assertIs(module.UNDO_HANDLERS[path], handler)
                self.assertIn(path, prepared_paths(module))

    def test_duplicate_paths_are_rejected_even_with_the_same_handler(self):
        handler = next(iter(build_undo_handlers().values()))
        with self.assertRaisesRegex(ValueError, "Duplicate undo handler"):
            merge_undo_handlers({"synthetic": handler}, {"synthetic": handler})

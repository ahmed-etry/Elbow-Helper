"""Shared patch targets for request orchestration collaborators."""

from contextlib import contextmanager, ExitStack
from importlib import import_module
from unittest.mock import patch


@contextmanager
def patch_engine(name, *args, **kwargs):
    with ExitStack() as stack:
        mocks = []
        for area in ("service", "rounds", "steps", "flow", "tool_call"):
            module = import_module("elbow_helper.features.agent.engine." + area)
            if hasattr(module, name):
                mocks.append(stack.enter_context(patch.object(module, name, *args, **kwargs)))
        yield mocks[0]

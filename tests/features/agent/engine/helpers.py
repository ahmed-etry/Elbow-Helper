"""Shared patch targets for request orchestration collaborators."""

from contextlib import contextmanager, ExitStack
from importlib import import_module
from unittest.mock import patch
from dataclasses import replace
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentToolDefinition


@contextmanager
def patch_engine(name, *args, **kwargs):
    with ExitStack() as stack:
        mocks = []
        for area in ("service", "rounds", "steps", "flow", "tool_call"):
            module = import_module("elbow_helper.features.agent.engine." + area)
            if hasattr(module, name):
                mocks.append(stack.enter_context(patch.object(module, name, *args, **kwargs)))
        yield mocks[0]


def patch_contracts(registry, contracts):
    replacements = {}
    for name, contract in contracts.items():
        tool = registry.get(name)
        if tool is None:
            tool = RegisteredAgentTool(AgentToolDefinition(name, "Synthetic", {}), None)
        replacements[name] = replace(tool, contract=contract)
    return patch.dict(registry, replacements)

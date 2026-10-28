"""Shared patch targets for request orchestration collaborators."""

from contextlib import contextmanager, ExitStack
from importlib import import_module
from unittest.mock import patch
from dataclasses import replace
from elbow_helper.agent.discord_actions.gif_library import ITEM_BY_ID
from elbow_helper.agent.engine.social_reply import MEME_SEND_FIT
from elbow_helper.agent.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentToolDefinition
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage


class RoutingSession:
    """Script route judgments independently from the answering session."""

    def __init__(self, decision="research"):
        self.decision = decision
        self.calls = []

    async def advance(self, **kwargs):
        self.calls.append(kwargs)
        return AgentStep(self.decision, (), AgentUsage(100, 1, 0, 100))


@contextmanager
def patch_engine(name, *args, **kwargs):
    with ExitStack() as stack:
        mocks = []
        for area in ("service", "rounds", "steps", "flow", "tool_call"):
            module = import_module("elbow_helper.agent.engine." + area)
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


def social_step(text="Synthetic reply.", *, form="text", meme_id=None, fit=MEME_SEND_FIT, reactions=(), call_id="social", **extra):
    """Script the required social output contract without a live model."""
    import json
    from elbow_helper.infrastructure.ai import AgentToolCall
    shortlist = [meme_id] if meme_id is not None else []
    shortlist.extend(item for item in ITEM_BY_ID if item != meme_id)
    return AgentStep("", (AgentToolCall(call_id, "submit_social_reply", json.dumps({
        "mechanic_check": "no_gameplay_number", "reply_form": form, "text": text,
        "reaction_fit": bool(reactions), "reaction_basis": "Synthetic semantic connection." if reactions else "",
        "gif_requested": False, "meme_shortlist": [
            {"id": item, "fit": fit if item == meme_id else 1} for item in shortlist[:3]],
        "reactions": list(reactions), **extra,
    })),), AgentUsage())


class LegacyCatalogueMixin:
    """Exercise specialist execution with explicitly installed legacy tools.

    These scripted plans test the handlers/executor, not model discovery. Family
    catalogue tests separately exercise the production default and discovery.
    """
    def run(self, result=None):
        import sys
        from elbow_helper.agent.engine import service, registry
        original = registry.build_agent_tools
        def installed(*args, **kwargs):
            return {name: replace(tool, default_visible=True)
                    for name, tool in original(*args, **kwargs).items()}
        with ExitStack() as stack:
            stack.enter_context(patch.object(service, 'build_agent_tools', installed))
            for name, module in tuple(sys.modules.items()):
                if name.startswith('agent.') and getattr(module, 'build_agent_tools', None) is original:
                    stack.enter_context(patch.object(module, 'build_agent_tools', installed))
            return super().run(result)

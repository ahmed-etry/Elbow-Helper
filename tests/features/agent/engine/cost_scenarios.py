"""Offline cost replay. Run with PYTHONPATH=tests from the checkout under test.

python -m features.agent.engine.cost_scenarios

Responses and tool results are scripted so this measures context cost and engine
rounds, not a model's ability to select tools. Tokens use the engine's documented
heuristic, not a provider tokenizer, cache accounting or a currency estimate.
This module also runs unchanged against the pre-routing checkout.
"""

import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import patch

from elbow_helper.features.agent.conversation.context import estimate_tokens
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage
from features.agent.engine.test_agent_plan_flow import _context


CLAN = {
    "source": "clash_api", "kind": "clan",
    "items": [{"tag": "#P0Y", "status": "ok", "data": {
        "name": "Example Clan", "members": 2,
        "memberList": [{"tag": "#P0L", "name": "Alpha"}, {"tag": "#P0Q", "name": "Beta"}],
    }}],
    "flags": {"status": "complete", "truncated": False},
}
KNOWLEDGE = {
    "sections": [{"title": "Example rules", "body": "Activity decisions require war evidence."}],
    "flags": {"status": "complete", "truncated": False},
}


def planned(name, arguments, identity):
    return AgentStep("", (AgentToolCall(identity, "submit_request_plan", json.dumps({
        "goal": "Answer the synthetic request", "effort": "low", "output": "text",
        "steps": [{"id": identity, "capability": name, "arguments": arguments, "depends_on": []}],
    })),), AgentUsage())


def scenarios():
    read = planned("read_clash", {"kind": "clan", "tags": ["#P0Y"]}, "clan")
    policy = planned("search_approved_knowledge", {"query": "Example rules"}, "rules")
    return (
        ("count", "How many members are in #P0Y?", "", [read, "2"], "2"),
        ("extraction", "Extract names from #P0Y, names only", "", [read], "Alpha\nBeta"),
        ("correction", "No, names only for #P0Y", "Earlier request asked for names without commentary.",
         [read], "Alpha\nBeta"),
        ("multi-source", "Compare the clan roster with the activity rules", "", [read, policy,
         "The roster lists Alpha and Beta. Activity decisions still need war evidence."],
         "The roster lists Alpha and Beta. Activity decisions still need war evidence."),
    )


class MeasuredModel:
    """Count the serialized request history with one stable heuristic."""

    context_window_tokens = 1_000_000

    def __init__(self, steps):
        self.steps = list(steps)
        self.rounds = []
        self.history = ""
        self.tools = ()

    def create_agent_session(self, **request):
        self.history = request["system_prompt"] + "\n" + request["prompt"]
        self.tools = request["tools"]
        return self

    def replace_tools(self, tools):
        self.tools = tools

    async def advance(self, results=(), *, allow_tools=True, reasoning_effort=None,
                      max_output_tokens=None, continuation_instruction=None):
        self.history += json.dumps([asdict(result) for result in results], ensure_ascii=False)
        self.history += continuation_instruction or ""
        prompt = estimate_tokens(self.history + json.dumps(
            [asdict(tool) for tool in self.tools], ensure_ascii=False,
        )) + 512
        step = self.steps.pop(0)
        if isinstance(step, str):
            step = AgentStep(step, (), AgentUsage())
        serialized = json.dumps({"content": step.content,
                                 "tool_calls": [asdict(call) for call in step.tool_calls]},
                                ensure_ascii=False)
        completion = estimate_tokens(serialized)
        assert completion <= max_output_tokens
        self.history += serialized
        self.rounds.append({"input": prompt, "output": completion,
                            "effort": reasoning_effort.value})
        return replace(step, usage=AgentUsage(prompt, completion))


class FixedClock:
    @staticmethod
    def now(tz=None):
        return datetime(2026, 1, 1, tzinfo=timezone.utc)


async def measure():
    registry = build_agent_tools()
    async def clan(*_):
        return CLAN
    async def knowledge(*_):
        return KNOWLEDGE
    registry["read_clash"] = replace(registry["read_clash"], handler=clan)
    registry["search_approved_knowledge"] = replace(registry["search_approved_knowledge"], handler=knowledge)
    # Fixed, small synthetic context. No deployment data or member identities.
    guide = SimpleNamespace(for_levels=lambda _: "synthetic.accounts: tag TEXT, name TEXT, clan_tag TEXT")
    store = SimpleNamespace(load=lambda: SimpleNamespace(prompt=lambda _: SimpleNamespace(
        text="Example rules: activity decisions require war evidence.", required_access=set(),
    )))
    measurements = {}
    for name, question, history, steps, expected in scenarios():
        model = MeasuredModel(steps)
        context = replace(_context(), knowledge_store=store)
        with (
            patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry),
            patch("elbow_helper.features.agent.engine.service.datetime", FixedClock),
        ):
            answer = await AgentService(model, data_guide=guide).answer(
                question=question, local_context="", context=context, conversation_history=history,
            )
        assert answer == expected, (name, answer, expected)
        assert not model.steps or (name == "count" and model.steps == [expected]), (
            name, "unexpected unused scripted responses",
        )
        measurements[name] = {
            "input_tokens_estimated": sum(item["input"] for item in model.rounds),
            "output_tokens_estimated": sum(item["output"] for item in model.rounds),
            "rounds": len(model.rounds), "first_effort": model.rounds[0]["effort"],
            "answer": answer,
        }
    return measurements


if __name__ == "__main__":
    print(json.dumps(asyncio.run(measure()), indent=2))

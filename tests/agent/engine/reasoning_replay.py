"""Replay admission against archived usage, without a provider or private fixtures.

Run with PYTHONPATH=tests:
python -m agent.engine.reasoning_replay PATH_TO_RUNS REQUEST_ID ...

This measures budget headroom, not new model answers. Archived final text and
member identities are never emitted. Input totals include the routing round.
"""

import json
from pathlib import Path
import sys

from elbow_helper.agent.engine.budgets import ContextBudget, RequestLimits, RESEARCH_LIMITS
from elbow_helper.infrastructure.ai import AgentUsage


PREVIOUS_LIMITS = RequestLimits(6, 120_000, 24_000, 8192)


def replay(record, policy):
    budget = ContextBudget(None, policy.round_output_tokens, 0, request_limits=policy)
    first_answer_only = None
    for index, row in enumerate(record["rounds"], 1):
        usage = row.get("usage") or {}
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if prompt is None or completion is None:
            raise ValueError("Replay requires complete archived usage")
        if row.get("phase") != "routing" and not budget.request_can_research(prompt):
            first_answer_only = first_answer_only or row.get("round", index)
        budget.observe(AgentUsage(prompt, completion), projected_input=prompt)
    # A next prompt cannot be smaller than the last prompt plus its completion.
    projected = budget.next_input_estimate
    return {"first_answer_only_round": first_answer_only,
            "spent_input": budget.spent_input, "spent_output": budget.spent_output,
            "spent_rounds": budget.spent_rounds,
            "can_research_again": budget.request_can_research(projected),
            "remaining_input": max(0, policy.input_tokens - budget.spent_input)}


def measure(directory, request_ids):
    results = {}
    for identity in request_ids:
        paths = list(Path(directory).glob(f"*-{identity}.json"))
        if len(paths) != 1:
            raise ValueError(f"Expected one archived run for {identity}")
        record = json.loads(paths[0].read_text(encoding="utf-8-sig"))
        results[identity] = {"previous": replay(record, PREVIOUS_LIMITS),
                             "revised": replay(record, RESEARCH_LIMITS)}
    return results


if __name__ == "__main__":
    print(json.dumps(measure(sys.argv[1], sys.argv[2:]), indent=2))

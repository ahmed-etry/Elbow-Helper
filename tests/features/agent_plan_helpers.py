"""Synthetic plan calls for service flow tests."""

import json

from elbow_helper.infrastructure.ai import AgentToolCall


def plan_call(
    *calls: AgentToolCall, periods=(), entities=(), effort="low",
    output="text", dependencies=None,
) -> AgentToolCall:
    steps = [
        {
            "id": call.call_id, "capability": call.name,
            "arguments": json.loads(call.arguments),
            "reason": "Read the selected value",
            "depends_on": list((dependencies or {}).get(call.call_id, ())),
        }
        for call in calls
    ]
    return AgentToolCall("plan-" + calls[0].call_id, "submit_request_plan", json.dumps({
        "goal": "Answer from selected values", "effort": effort, "output": output,
        "periods": list(periods), "entities": list(entities), "steps": steps,
    }))

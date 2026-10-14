"""Request diagnostics retained only for the operator transcript archive."""

from dataclasses import asdict, dataclass, field
import json
from typing import Any

from ..plan.format import PLAN_TOOL_NAME


@dataclass(slots=True)
class OperatorDebugRecord:
    question: str
    image_labels: list[str] = field(default_factory=list)
    plans: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    rounds: list[dict[str, Any]] = field(default_factory=list)
    answer: str | None = None
    status: str = "incomplete"

    def submitted(self, calls):
        for call in calls:
            if call.name != PLAN_TOOL_NAME:
                continue
            try:
                plan = json.loads(call.arguments)
            except (TypeError, ValueError):
                plan = None
            self.plans.append({"call_id": call.call_id, "arguments": call.arguments,
                               "plan": plan, "check": {"status": "not_checked"}})

    def checked(self, call_id, check):
        for plan in reversed(self.plans):
            if plan["call_id"] == call_id:
                plan["check"] = asdict(check)
                break

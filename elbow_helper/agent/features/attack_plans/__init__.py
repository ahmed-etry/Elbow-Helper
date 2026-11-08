"""Attack plans agent capabilities."""

from elbow_helper.agent.features.attack_plans.commands import attack_plan_adapters


TOOLS = ()

COMMAND_ADAPTERS = (
    *attack_plan_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

"""Attack plans agent capabilities."""

from .commands import attack_plan_adapters

TOOLS = ()

COMMAND_ADAPTERS = (
    *attack_plan_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

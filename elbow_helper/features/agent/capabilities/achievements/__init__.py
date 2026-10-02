"""Achievements agent capabilities."""

from .reads import achievement_tools
from .raffle_purchase import raffle_purchase_tools
from .economy import achievement_economy_tools
from .commands import achievement_adapters
from .raffle_commands import raffle_adapters

TOOLS = (
    *achievement_tools(),
    *raffle_purchase_tools(),
    *achievement_economy_tools(),
)

COMMAND_ADAPTERS = (
    *achievement_adapters(),
    *raffle_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

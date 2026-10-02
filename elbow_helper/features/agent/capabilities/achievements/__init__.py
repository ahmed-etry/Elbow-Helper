"""Achievements agent capabilities."""

from .reads import achievement_tools
from .raffle_purchase import raffle_purchase_tools
from .economy import achievement_economy_tools
from .commands import achievement_adapters
from .raffle_commands import raffle_adapters
from ...actions.undo import merge_undo_handlers
from .raffle_commands import UNDO_HANDLERS as RAFFLE_COMMANDS_UNDO_HANDLERS


TOOLS = (
    *achievement_tools(),
    *raffle_purchase_tools(),
    *achievement_economy_tools(),
)

COMMAND_ADAPTERS = (
    *achievement_adapters(),
    *raffle_adapters(),
)


UNDO_HANDLERS = merge_undo_handlers(
    RAFFLE_COMMANDS_UNDO_HANDLERS,
)

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

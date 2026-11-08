"""Achievements agent capabilities."""

from elbow_helper.agent.features.achievements.reads import achievement_tools
from elbow_helper.agent.features.achievements.raffle_purchase import raffle_purchase_tools
from elbow_helper.agent.features.achievements.economy import achievement_economy_tools
from elbow_helper.agent.features.achievements.commands import achievement_adapters
from elbow_helper.agent.features.achievements.raffle_commands import raffle_adapters
from elbow_helper.agent.actions.undo import merge_undo_handlers
from elbow_helper.agent.features.achievements.raffle_commands import UNDO_HANDLERS as RAFFLE_COMMANDS_UNDO_HANDLERS


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

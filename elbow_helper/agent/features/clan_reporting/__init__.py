"""Clan reporting agent capabilities."""

from elbow_helper.agent.features.clan_reporting.reads import clan_reporting_tools
from elbow_helper.agent.features.clan_reporting.elder_board import missing_elder_board_tools


TOOLS = (
    *clan_reporting_tools(),
    *missing_elder_board_tools(),
)

COMMAND_ADAPTERS = ()

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

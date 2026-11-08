"""Clan transfers agent capabilities."""

from elbow_helper.agent.features.clan_transfers.reads import transfer_tools
from elbow_helper.agent.features.clan_transfers.queue import transfer_management_tools
from elbow_helper.agent.features.clan_transfers.commands import clan_transfer_adapters


TOOLS = (
    *transfer_tools(),
    *transfer_management_tools(),
)

COMMAND_ADAPTERS = (
    *clan_transfer_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

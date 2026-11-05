"""Clan transfers agent capabilities."""

from .reads import transfer_tools
from .queue import transfer_management_tools
from .commands import clan_transfer_adapters


TOOLS = (
    *transfer_tools(),
    *transfer_management_tools(),
)

COMMAND_ADAPTERS = (
    *clan_transfer_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

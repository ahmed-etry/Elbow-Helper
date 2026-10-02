"""Clan health agent capabilities."""

from .reads import clan_health_tools
from .settings import clan_health_settings_tools
from .commands import health_adapters

TOOLS = (
    *clan_health_tools(),
    *clan_health_settings_tools(),
)

COMMAND_ADAPTERS = (
    *health_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

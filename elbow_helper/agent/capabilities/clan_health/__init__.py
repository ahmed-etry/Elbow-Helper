"""Clan health agent capabilities."""

from .settings import clan_health_settings_tools
from .commands import health_adapters
from ...actions.undo import merge_undo_handlers
from .settings import UNDO_HANDLERS as SETTINGS_UNDO_HANDLERS


TOOLS = (
    *clan_health_settings_tools(),
)

COMMAND_ADAPTERS = (
    *health_adapters(),
)


UNDO_HANDLERS = merge_undo_handlers(
    SETTINGS_UNDO_HANDLERS,
)

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

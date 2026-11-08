"""Clan health agent capabilities."""

from elbow_helper.agent.tool_support.adapters.clan_health.settings import clan_health_settings_tools
from elbow_helper.agent.tool_support.adapters.clan_health.commands import health_adapters
from elbow_helper.agent.actions.undo import merge_undo_handlers
from elbow_helper.agent.tool_support.adapters.clan_health.settings import UNDO_HANDLERS as SETTINGS_UNDO_HANDLERS


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

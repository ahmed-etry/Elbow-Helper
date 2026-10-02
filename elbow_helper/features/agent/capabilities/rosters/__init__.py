"""Rosters agent capabilities."""

from .reads import roster_tools
from .management import roster_management_tools
from .signups import roster_account_management_tools
from .setup_commands import roster_adapters
from .timing_commands import timing_adapters
from .post_commands import post_adapters

TOOLS = (
    *roster_tools(),
    *roster_management_tools(),
    *roster_account_management_tools(),
)

COMMAND_ADAPTERS = (
    *roster_adapters(),
    *timing_adapters(),
    *post_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

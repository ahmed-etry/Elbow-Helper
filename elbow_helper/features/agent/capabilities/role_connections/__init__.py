"""Role connections agent capabilities."""

from .reads import role_connection_tools
from .management import role_connection_management_tools
from .scan import role_connection_scan_tools
from .commands import role_connection_adapters

TOOLS = (
    *role_connection_tools(),
    *role_connection_management_tools(),
    *role_connection_scan_tools(),
)

COMMAND_ADAPTERS = (
    *role_connection_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

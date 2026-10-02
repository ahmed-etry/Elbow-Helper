"""Role connections agent capabilities."""

from .reads import role_connection_tools
from .management import role_connection_management_tools
from .scan import role_connection_scan_tools
from .commands import role_connection_adapters
from ...actions.undo import merge_undo_handlers
from .management import UNDO_HANDLERS as MANAGEMENT_UNDO_HANDLERS
from .scan import UNDO_HANDLERS as SCAN_UNDO_HANDLERS


TOOLS = (
    *role_connection_tools(),
    *role_connection_management_tools(),
    *role_connection_scan_tools(),
)

COMMAND_ADAPTERS = (
    *role_connection_adapters(),
)


UNDO_HANDLERS = merge_undo_handlers(
    MANAGEMENT_UNDO_HANDLERS,
    SCAN_UNDO_HANDLERS,
)

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

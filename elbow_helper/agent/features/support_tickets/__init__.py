"""Support tickets agent capabilities."""

from elbow_helper.agent.features.support_tickets.reads import support_tools
from elbow_helper.agent.features.support_tickets.reopen import support_reopen_tools
from elbow_helper.agent.features.support_tickets.commands import support_ticket_adapters


TOOLS = (
    *support_tools(),
    *support_reopen_tools(),
)

COMMAND_ADAPTERS = (
    *support_ticket_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

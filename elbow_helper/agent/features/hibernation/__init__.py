"""Hibernation agent capabilities."""

from elbow_helper.agent.features.hibernation.reads import hibernation_tools
from elbow_helper.agent.features.hibernation.tickets import reactivation_ticket_tools
from elbow_helper.agent.features.hibernation.commands import hibernation_adapters


TOOLS = (
    *hibernation_tools(),
    *reactivation_ticket_tools(),
)

COMMAND_ADAPTERS = (
    *hibernation_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

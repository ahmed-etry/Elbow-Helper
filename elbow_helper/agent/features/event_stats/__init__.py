"""Events agent capabilities."""

from elbow_helper.agent.features.event_stats.reads import event_tools
from elbow_helper.agent.features.event_stats.management import event_management_tools
from elbow_helper.agent.features.event_stats.commands import event_adapters


TOOLS = (
    *event_tools(),
    *event_management_tools(),
)

COMMAND_ADAPTERS = (
    *event_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

"""Member lifecycle agent capabilities."""

from elbow_helper.agent.features.member_lifecycle.reads import member_lifecycle_tools


TOOLS = (
    *member_lifecycle_tools(),
)

COMMAND_ADAPTERS = ()

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

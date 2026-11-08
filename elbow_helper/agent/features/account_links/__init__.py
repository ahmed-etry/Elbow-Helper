"""Account links agent capabilities."""

from elbow_helper.agent.features.account_links.suggestions import account_suggestion_tools
from elbow_helper.agent.features.account_links.commands import account_link_adapters
from elbow_helper.agent.reads.people_tools import account_read_tools
from elbow_helper.agent.actions.undo import merge_undo_handlers
from elbow_helper.agent.features.account_links.commands import UNDO_HANDLERS as COMMANDS_UNDO_HANDLERS


TOOLS = (
    *account_read_tools(),
    *account_suggestion_tools(),
)

COMMAND_ADAPTERS = (
    *account_link_adapters(),
)


UNDO_HANDLERS = merge_undo_handlers(
    COMMANDS_UNDO_HANDLERS,
)

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

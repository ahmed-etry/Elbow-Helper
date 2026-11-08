"""Rosters agent capabilities."""

from elbow_helper.agent.features.rosters.management import roster_management_tools
from elbow_helper.agent.features.rosters.signups import roster_account_management_tools
from elbow_helper.agent.features.rosters.setup_commands import roster_adapters
from elbow_helper.agent.features.rosters.timing_commands import timing_adapters
from elbow_helper.agent.features.rosters.post_commands import post_adapters
from elbow_helper.agent.actions.undo import merge_undo_handlers
from elbow_helper.agent.features.rosters.management import UNDO_HANDLERS as MANAGEMENT_UNDO_HANDLERS


TOOLS = (
    *roster_management_tools(),
    *roster_account_management_tools(),
)

COMMAND_ADAPTERS = (
    *roster_adapters(),
    *timing_adapters(),
    *post_adapters(),
)


UNDO_HANDLERS = merge_undo_handlers(
    MANAGEMENT_UNDO_HANDLERS,
)

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

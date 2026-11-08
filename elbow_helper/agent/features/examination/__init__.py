"""Examination agent capabilities."""

from elbow_helper.agent.features.examination.reads import examination_tools
from elbow_helper.agent.features.examination.examiner_profile import examiner_profile_tools
from elbow_helper.agent.features.examination.promotion_route import promotion_route_tools
from elbow_helper.agent.actions.undo import merge_undo_handlers
from elbow_helper.agent.features.examination.examiner_profile import UNDO_HANDLERS as EXAMINER_PROFILE_UNDO_HANDLERS


TOOLS = (
    *examination_tools(),
    *examiner_profile_tools(),
    *promotion_route_tools(),
)

COMMAND_ADAPTERS = ()


UNDO_HANDLERS = merge_undo_handlers(
    EXAMINER_PROFILE_UNDO_HANDLERS,
)

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

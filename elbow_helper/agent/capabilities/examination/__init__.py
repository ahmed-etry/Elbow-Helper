"""Examination agent capabilities."""

from .reads import examination_tools
from .examiner_profile import examiner_profile_tools
from .promotion_route import promotion_route_tools
from ...actions.undo import merge_undo_handlers
from .examiner_profile import UNDO_HANDLERS as EXAMINER_PROFILE_UNDO_HANDLERS


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

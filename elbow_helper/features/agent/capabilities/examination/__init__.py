"""Examination agent capabilities."""

from .reads import examination_tools
from .examiner_profile import examiner_profile_tools
from .promotion_route import promotion_route_tools

TOOLS = (
    *examination_tools(),
    *examiner_profile_tools(),
    *promotion_route_tools(),
)

COMMAND_ADAPTERS = ()

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

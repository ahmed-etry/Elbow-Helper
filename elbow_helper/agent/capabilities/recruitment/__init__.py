"""Recruitment agent capabilities."""

from .reads import recruitment_tools
from .trial_end import trial_end_tools
from .commands import recruitment_adapters
from .leagues import cohort_league_tools


TOOLS = (
    *recruitment_tools(),
    *cohort_league_tools(),
    *trial_end_tools(),
)

COMMAND_ADAPTERS = (
    *recruitment_adapters(),
)

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

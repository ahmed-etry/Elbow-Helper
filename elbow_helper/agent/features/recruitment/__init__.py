"""Recruitment agent capabilities."""

from elbow_helper.agent.features.recruitment.reads import recruitment_tools
from elbow_helper.agent.features.recruitment.trial_end import trial_end_tools
from elbow_helper.agent.features.recruitment.commands import recruitment_adapters
from elbow_helper.agent.features.recruitment.leagues import cohort_league_tools


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

"""Leadership news agent capabilities."""

from elbow_helper.agent.features.leadership_news.actions import leadership_news_tools


TOOLS = (
    *leadership_news_tools(),
)

COMMAND_ADAPTERS = ()

UNDO_HANDLERS = {}

__all__ = ["TOOLS", "COMMAND_ADAPTERS", "UNDO_HANDLERS"]

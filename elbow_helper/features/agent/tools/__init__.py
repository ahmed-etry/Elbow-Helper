"""Fixed read-tool catalogue for the Core agent beta."""

from __future__ import annotations

from .clan_health import clan_health_tools
from .discord import discord_tools
from .members import member_tools
from ..models import RegisteredAgentTool


def build_agent_tools() -> dict[str, RegisteredAgentTool]:
    """Build the complete catalogue without exposing arbitrary capabilities."""

    tools = (*discord_tools(), *member_tools(), *clan_health_tools())
    return {tool.definition.name: tool for tool in tools}


__all__ = ["build_agent_tools"]

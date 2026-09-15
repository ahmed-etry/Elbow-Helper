"""Internal models for the read-only Core agent."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from typing import Mapping

import discord
from discord.ext import commands

from elbow_helper.discord.message_search import DiscordMessageSearch
from elbow_helper.infrastructure.ai import AgentToolDefinition


@dataclass(frozen=True, slots=True)
class AgentRequestContext:
    """Trusted runtime objects available to bounded agent tools."""

    bot: commands.Bot
    guild: discord.Guild
    member: discord.Member
    source_message: discord.Message
    account_links: Any
    clan_health: Any
    message_search: DiscordMessageSearch


AgentToolHandler = Callable[
    [AgentRequestContext, Mapping[str, Any]],
    Awaitable[Mapping[str, Any]],
]


@dataclass(frozen=True, slots=True)
class RegisteredAgentTool:
    """A model-facing definition paired with one trusted handler."""

    definition: AgentToolDefinition
    handler: AgentToolHandler

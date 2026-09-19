"""Fixed read-tool catalogue for the Core agent beta."""

from __future__ import annotations

from .attachments import attachment_tools
from .achievements import achievement_tools
from .achievement_economy import achievement_economy_tools
from .clan_health import clan_health_tools
from .clan_reporting import clan_reporting_tools
from .cwl import cwl_tools
from .discord import discord_tools
from .examination import examination_tools
from .events import event_tools
from .hibernation import hibernation_tools
from .history import history_tools
from .knowledge import knowledge_tools
from .member_lifecycle import member_lifecycle_tools
from .members import member_tools
from .research import research_tools
from .records import record_tools
from .recruitment import recruitment_tools
from .roles import role_tools
from .role_connections import role_connection_tools
from .rosters import roster_tools
from .spreadsheets import spreadsheet_tools
from .support import support_tools
from .threads import thread_tools
from .transfers import transfer_tools
from .wars import war_tools
from .working_state import working_state_tools
from ..models import RegisteredAgentTool


def build_agent_tool_groups() -> dict[str, tuple[RegisteredAgentTool, ...]]:
    """Build cohesive capability groups for model-directed discovery."""

    return {
        "discord_research": (*discord_tools(), *thread_tools(), *research_tools()),
        "members_roles": (
            *member_tools(), *role_tools(), *role_connection_tools(),
        ),
        "achievements_events": (
            *achievement_tools(), *achievement_economy_tools(), *event_tools(),
        ),
        "clan_operations": (*clan_reporting_tools(), *clan_health_tools()),
        "wars": war_tools(),
        "rosters": roster_tools(),
        "cwl": cwl_tools(),
        "transfers": transfer_tools(),
        "member_cases": (
            *member_lifecycle_tools(), *hibernation_tools(), *support_tools(),
            *recruitment_tools(), *examination_tools(), *record_tools(),
        ),
        "files": attachment_tools(),
        "knowledge_history": (*history_tools(), *knowledge_tools()),
        "planning_output": (*working_state_tools(), *spreadsheet_tools()),
    }


def build_agent_tools() -> dict[str, RegisteredAgentTool]:
    """Build the complete catalogue without exposing arbitrary capabilities."""

    tools = tuple(
        tool
        for group in build_agent_tool_groups().values()
        for tool in group
    )
    return {tool.definition.name: tool for tool in tools}


__all__ = ["build_agent_tool_groups", "build_agent_tools"]

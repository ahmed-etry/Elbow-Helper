"""Fixed read-tool catalogue for the agent."""

from __future__ import annotations
from dataclasses import replace

from .attachments import attachment_tools
from .action_log import action_log_tools
from .achievements import achievement_tools
from .achievement_economy import achievement_economy_tools
from .clan_health import clan_health_tools
from .clan_reporting import clan_reporting_tools
from .commands import command_tools
from .cwl import cwl_tools
from .cwl_bonus_review import cwl_bonus_review_tools
from .discord import discord_tools
from .discord_roles import discord_role_tools
from .discord_messages import discord_message_tools
from .discord_threads import discord_thread_tools
from .discord_message_controls import discord_message_control_tools
from .discord_nicknames import discord_nickname_tools
from .examination import examination_tools
from .events import event_tools
from .event_management import event_management_tools
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
from .role_connection_management import role_connection_management_tools
from .role_connection_scan import role_connection_scan_tools
from .rosters import roster_tools
from .roster_management import roster_management_tools
from .roster_account_management import roster_account_management_tools
from .saved_reports import replace_report_tools
from .spreadsheets import spreadsheet_tools
from .support import support_tools
from .threads import thread_tools
from .transfers import transfer_tools
from .transfer_management import transfer_management_tools
from .wars import war_tools
from .working_state import working_state_tools
from ..models import RegisteredAgentTool
from ..actions.contracts import ActionClass
from ..models import AgentCapabilityEffect
from ..capabilities import validate_contract_catalogue
from ..plan.results import result_handler


def build_agent_tool_groups() -> dict[str, tuple[RegisteredAgentTool, ...]]:
    """Group the registered read capabilities."""

    return {
        "commands": command_tools(),
        "discord_research": (*discord_tools(), *thread_tools(), *research_tools()),
        "discord_actions": (*discord_message_tools(), *discord_thread_tools(),
                            *discord_message_control_tools(), *discord_nickname_tools()),
        "members_roles": (
            *member_tools(), *role_tools(), *role_connection_tools(),
            *role_connection_management_tools(),
            *role_connection_scan_tools(),
            *discord_role_tools(),
        ),
        "achievements_events": (
            *achievement_tools(), *achievement_economy_tools(), *event_tools(),
            *event_management_tools(),
        ),
        "clan_operations": (*clan_reporting_tools(), *clan_health_tools()),
        "wars": war_tools(),
        "rosters": (*roster_tools(), *roster_management_tools(),
                    *roster_account_management_tools()),
        "cwl": (*cwl_tools(), *cwl_bonus_review_tools()),
        "transfers": (*transfer_tools(), *transfer_management_tools()),
        "member_cases": (
            *member_lifecycle_tools(), *hibernation_tools(), *support_tools(),
            *recruitment_tools(), *examination_tools(), *record_tools(),
        ),
        "files": attachment_tools(),
        "knowledge_history": (*history_tools(), *knowledge_tools(), *action_log_tools()),
        "planning_output": (*working_state_tools(), *spreadsheet_tools()),
    }


def build_agent_tools() -> dict[str, RegisteredAgentTool]:
    """Build the complete catalogue without exposing arbitrary capabilities."""

    tools = replace_report_tools(tuple(
        tool
        for group in build_agent_tool_groups().values()
        for tool in group
    ))
    registry = {tool.definition.name: tool for tool in tools}
    if len(registry) != len(tools):
        raise ValueError("Duplicate agent capability")
    validate_contract_catalogue(registry)
    return {name: replace(
        tool, handler=result_handler(tool.handler),
        action_class=(ActionClass.OUTPUT if tool.effect is AgentCapabilityEffect.ARTIFACT
                      else tool.action_class),
    )
            for name, tool in registry.items()}


__all__ = ["build_agent_tool_groups", "build_agent_tools"]

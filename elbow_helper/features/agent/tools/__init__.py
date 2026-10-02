"""Fixed read-tool catalogue for the agent."""

from __future__ import annotations
from dataclasses import replace

from ..files.attachment_tools import attachment_tools
from ..actions.log_tools import action_log_tools
from ..capabilities.account_links.suggestions import account_suggestion_tools
from ..capabilities.achievements.reads import achievement_tools
from ..capabilities.achievements.raffle_purchase import raffle_purchase_tools
from ..capabilities.achievements.economy import achievement_economy_tools
from ..capabilities.clan_health.reads import clan_health_tools
from ..capabilities.clan_health.settings import clan_health_settings_tools
from ..capabilities.clan_reporting.reads import clan_reporting_tools
from ..commands.help_tool import command_tools
from ..capabilities.cwl.reads import cwl_tools
from ..capabilities.cwl.bonus_review import cwl_bonus_review_tools
from ..capabilities.cwl.bonus_scoring import cwl_bonus_scoring_tools
from ..capabilities.cwl.cc_status import cwl_cc_status_tools
from ..capabilities.cwl.prep_board import cwl_prep_refresh_tools
from ..capabilities.cwl.member_hub import cwl_member_hub_tools
from ..research.history import discord_tools
from ..discord_actions.roles import discord_role_tools
from ..discord_actions.messages import discord_message_tools
from ..discord_actions.threads import discord_thread_tools
from ..discord_actions.message_controls import discord_message_control_tools
from ..discord_actions.nicknames import discord_nickname_tools
from ..capabilities.examination.reads import examination_tools
from ..capabilities.examination.examiner_profile import examiner_profile_tools
from ..capabilities.events.reads import event_tools
from ..capabilities.events.management import event_management_tools
from ..capabilities.hibernation.reads import hibernation_tools
from ..conversation.history_tool import history_tools
from ..knowledge.tools import knowledge_tools
from ..capabilities.leadership_news.actions import leadership_news_tools
from ..capabilities.member_lifecycle.reads import member_lifecycle_tools
from ..capabilities.account_links.reads import member_tools
from ..capabilities.clan_reporting.elder_board import missing_elder_board_tools
from ..research.tools import research_tools
from ..capabilities.records.reads import record_tools
from ..capabilities.examination.promotion_route import promotion_route_tools
from ..capabilities.recruitment.reads import recruitment_tools
from ..capabilities.account_links.role_audit import role_tools
from ..capabilities.role_connections.reads import role_connection_tools
from ..capabilities.role_connections.management import role_connection_management_tools
from ..capabilities.role_connections.scan import role_connection_scan_tools
from ..capabilities.rosters.reads import roster_tools
from ..capabilities.rosters.management import roster_management_tools
from ..capabilities.rosters.signups import roster_account_management_tools
from ..reports.tools import replace_report_tools
from ..files.spreadsheet_tools import spreadsheet_tools
from ..capabilities.support_tickets.reads import support_tools
from ..research.threads import thread_tools
from ..capabilities.hibernation.tickets import reactivation_ticket_tools
from ..capabilities.support_tickets.reopen import support_reopen_tools
from ..capabilities.clan_transfers.reads import transfer_tools
from ..capabilities.recruitment.trial_end import trial_end_tools
from ..capabilities.clan_transfers.queue import transfer_management_tools
from ..capabilities.wars.reads import war_tools
from ..conversation.instruction_tools import working_state_tools
from ..scheduled.tools import standing_tools
from ..models import RegisteredAgentTool
from ..actions.contracts import ActionClass
from ..models import AgentCapabilityEffect
from ..engine.capability_contract import validate_contract_catalogue
from ..plan.results import result_handler


def build_agent_tool_groups() -> dict[str, tuple[RegisteredAgentTool, ...]]:
    """Group the registered read capabilities."""

    return {
        "commands": command_tools(),
        "discord_research": (*discord_tools(), *thread_tools(), *research_tools()),
        "discord_actions": (*discord_message_tools(), *discord_thread_tools(),
                            *discord_message_control_tools(), *discord_nickname_tools()),
        "members_roles": (
            *member_tools(), *role_tools(), *account_suggestion_tools(),
            *role_connection_tools(),
            *role_connection_management_tools(),
            *role_connection_scan_tools(),
            *discord_role_tools(),
        ),
        "achievements_events": (
            *achievement_tools(), *achievement_economy_tools(), *raffle_purchase_tools(), *event_tools(),
            *event_management_tools(),
        ),
        "clan_operations": (*clan_reporting_tools(), *clan_health_tools(),
                            *clan_health_settings_tools(),
                            *missing_elder_board_tools()),
        "wars": war_tools(),
        "rosters": (*roster_tools(), *roster_management_tools(),
                    *roster_account_management_tools()),
        "cwl": (*cwl_tools(), *cwl_member_hub_tools(), *cwl_bonus_review_tools(),
                *cwl_bonus_scoring_tools(), *cwl_prep_refresh_tools(),
                *cwl_cc_status_tools()),
        "transfers": (*transfer_tools(), *transfer_management_tools()),
        "member_cases": (
            *member_lifecycle_tools(), *hibernation_tools(), *support_tools(),
            *recruitment_tools(), *examination_tools(), *examiner_profile_tools(),
            *promotion_route_tools(), *support_reopen_tools(), *reactivation_ticket_tools(), *trial_end_tools(),
            *record_tools(),
        ),
        "files": attachment_tools(),
        "knowledge_history": (*history_tools(), *knowledge_tools(), *action_log_tools()),
        "news": leadership_news_tools(),
        "planning_output": (*working_state_tools(), *spreadsheet_tools()),
        "standing_rules": standing_tools(),
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

"""Collect the enabled read, output and change capabilities."""
from __future__ import annotations
from dataclasses import replace
from ..capabilities import FEATURES
from ..discord_actions import UNDO_HANDLERS as DISCORD_UNDO_HANDLERS
from ..actions.undo import merge_undo_handlers, UndoHandler
from ..files.attachment_tools import attachment_tools
from ..actions.log_tools import action_log_tools
from ..commands.help_tool import command_tools
from ..research.catalogue import discord_tools
from ..research.members import member_tools
from ..discord_actions.roles import discord_role_tools
from ..discord_actions.direct_messages import direct_message_tools
from ..discord_actions.reactions import reaction_tools
from ..discord_actions.message_tools import discord_message_tools
from ..discord_actions.threads import discord_thread_tools
from ..discord_actions.message_controls import discord_message_control_tools
from ..discord_actions.nicknames import discord_nickname_tools
from ..conversation.history_tool import history_tools
from ..knowledge.tools import knowledge_tools
from ..research.tools import research_tools
from ..reports.tools import replace_report_tools
from ..files.spreadsheet_tools import spreadsheet_tools
from ..research.threads import thread_tools
from ..conversation.instruction_tools import working_state_tools
from ..scheduled.tools import standing_tools
from ..datasets.tools import dataset_tools
from ..datasets.clash import clash_tools
from ..models import RegisteredAgentTool
from ..actions.contracts import ActionClass
from ..models import AgentCapabilityEffect
from .capability_contract import validate_contract_catalogue
from ..plan.results import result_handler
from .result_hints import RETURN_HINTS


def build_agent_tool_groups() -> dict[str, tuple[RegisteredAgentTool, ...]]:
    """Group the registered read capabilities."""

    return {
        "datasets": (*dataset_tools(), *clash_tools()),
        "commands": command_tools(),
        "discord_research": (
            *discord_tools(), *member_tools(), *thread_tools(), *research_tools(),
        ),
        "discord_actions": (
            *discord_message_tools(), *discord_thread_tools(), *discord_message_control_tools(),
            *discord_nickname_tools(), *discord_role_tools(),
            *direct_message_tools(), *reaction_tools(),
        ),
        "files": attachment_tools(),
        "knowledge_history": (*history_tools(), *knowledge_tools(), *action_log_tools()),
        "planning_output": (*working_state_tools(), *spreadsheet_tools()),
        "standing_rules": standing_tools(build_agent_tools),
        **{name: feature.TOOLS for name, feature in FEATURES.items()},
    }


def build_agent_tools() -> dict[str, RegisteredAgentTool]:
    """Build the complete catalogue without exposing arbitrary capabilities."""

    originals = tuple(
        tool
        for group in build_agent_tool_groups().values()
        for tool in group
    )
    original_registry = {tool.definition.name: tool for tool in originals}
    if len(original_registry) != len(originals):
        raise ValueError("Duplicate agent capability")
    validate_contract_catalogue(original_registry)
    tools = replace_report_tools(originals)
    registry = {tool.definition.name: tool for tool in tools}
    if len(registry) != len(tools):
        raise ValueError("Duplicate agent capability")
    validate_contract_catalogue(registry)
    return {name: replace(
        tool, handler=result_handler(tool.handler), returns=tool.returns or RETURN_HINTS.get(name),
        action_class=(ActionClass.OUTPUT if tool.effect is AgentCapabilityEffect.ARTIFACT
                      else tool.action_class),
    )
            for name, tool in registry.items()}


def build_undo_handlers() -> dict[str, UndoHandler]:
    return merge_undo_handlers(
        DISCORD_UNDO_HANDLERS,
        *(feature.UNDO_HANDLERS for feature in FEATURES.values()),
    )
__all__ = ["build_agent_tool_groups", "build_agent_tools", "build_undo_handlers"]

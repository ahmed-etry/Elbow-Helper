"""Mention-driven agent feature with separately configured rollout access."""

from __future__ import annotations
import asyncio

from elbow_helper.discord.message_search import DiscordMessageSearch
from elbow_helper.discord.thread_discovery import DiscordThreadDiscovery
from elbow_helper.configuration.guild import GUILD_ID

from .cog import AgentCog
from .conversation.transcripts import TranscriptArchive
from .conversation.repository import ConversationRepository
from .conversation.persistence import ConversationPersistence
from .research.repository import ResearchJobRepository
from .research.runner import ResearchJobRunner
from .actions.repository import AgentActionRepository
from .actions.runner import AgentActionRunner
from .tools.discord_roles import prepare_role_undo
from .tools.discord_messages import prepare_edit_undo
from .tools.discord_threads import prepare_thread_member_undo, prepare_thread_update_undo
from .tools.discord_message_controls import prepare_control_undo
from .tools.discord_nicknames import prepare_nickname_undo
from .commands.adapters.records import prepare_record_add_undo, prepare_record_edit_undo
from .commands.adapters.achievements import prepare_raffle_prize_undo
from .commands.adapters.account_links import prepare_account_add_undo
from .tools.roster_management import prepare_roster_layout_undo
from .tools.role_connection_management import prepare_role_connection_undo
from .tools.role_connection_scan import prepare_role_connection_scan_undo
from .tools.cwl_bonus_scoring import prepare_cwl_bonus_scoring_undo
from .tools.clan_health_settings import prepare_health_settings_undo
from .tools.examiner_profile import prepare_examiner_profile_undo
from .knowledge.store import KnowledgeStore


async def setup(bot) -> None:
    account_links = bot.get_cog("AccountLinks")
    clan_health = bot.get_cog("ClanHealth")
    clan_health_queries = getattr(clan_health, "queries", None)
    rosters = bot.get_cog("Rosters")
    cwl = bot.get_cog("CwlManagement")
    wars = bot.get_cog("WarManager")
    transfers = bot.get_cog("ClanTransfers")
    hibernation = bot.get_cog("Hibernate")
    support = bot.get_cog("SupportActions")
    recruitment = bot.get_cog("Recruitment")
    examination = bot.get_cog("Examination")
    records = bot.get_cog("Records")
    achievements = bot.get_cog("Achievements")
    event_stats = bot.get_cog("EventStatsCog")
    member_lifecycle = bot.get_cog("MemberLifecycle")
    clan_reporting = bot.get_cog("ClanReporting")
    role_connections = bot.get_cog("RoleConnections")
    cwl_queries = getattr(cwl, "queries", None)
    war_queries = getattr(wars, "queries", None)
    transfer_queries = getattr(transfers, "queries", None)
    hibernation_queries = getattr(hibernation, "queries", None)
    support_queries = getattr(support, "queries", None)
    recruitment_queries = getattr(recruitment, "queries", None)
    examination_queries = getattr(examination, "queries", None)
    record_queries = getattr(records, "queries", None)
    achievement_queries = getattr(achievements, "queries", None)
    event_queries = getattr(event_stats, "queries", None)
    member_lifecycle_queries = getattr(member_lifecycle, "queries", None)
    clan_reporting_queries = getattr(clan_reporting, "queries", None)
    role_connection_queries = getattr(role_connections, "queries", None)
    if (account_links is None or clan_health_queries is None or rosters is None
            or cwl_queries is None or war_queries is None or transfer_queries is None
            or hibernation_queries is None or support_queries is None
            or recruitment_queries is None or examination_queries is None
            or record_queries is None or achievement_queries is None
            or event_queries is None or member_lifecycle_queries is None
            or clan_reporting_queries is None
            or role_connection_queries is None):
        raise RuntimeError(
            "Agent requires AccountLinks, ClanHealth, Wars, Rosters, CWL, "
            "ClanTransfers, Hibernation, SupportActions, Recruitment, Examination, "
            "Records, Achievements, EventStats, MemberLifecycle, ClanReporting "
            "and RoleConnections"
        )
    archive = await asyncio.to_thread(TranscriptArchive, bot.paths.data_root / "agent" / "transcripts.sqlite3")
    repository = await asyncio.to_thread(ConversationRepository, bot.paths.data_root / "agent" / "agent.sqlite3")
    research_jobs = await asyncio.to_thread(
        ResearchJobRepository,
        bot.paths.data_root / "agent" / "research_jobs.sqlite3",
    )
    action_repository = await asyncio.to_thread(
        AgentActionRepository,
        bot.paths.data_root / "agent" / "actions.sqlite3",
    )
    message_search = DiscordMessageSearch(bot.http)
    thread_discovery = DiscordThreadDiscovery()
    research_runner = ResearchJobRunner(
        bot=bot, repository=research_jobs, message_search=message_search,
        guild_id=GUILD_ID,
    )
    action_runner = AgentActionRunner(
        bot=bot, repository=action_repository, guild_id=GUILD_ID,
        enabled=getattr(bot, "agent_actions_enabled", True),
    )
    action_runner.undo_handlers.update({
        "add_discord_roles": prepare_role_undo,
        "remove_discord_roles": prepare_role_undo,
        "undo_discord_role": prepare_role_undo,
        "edit_agent_message": prepare_edit_undo,
        "undo_agent_message_edit": prepare_edit_undo,
        "update_discord_thread": prepare_thread_update_undo,
        "undo_discord_thread_update": prepare_thread_update_undo,
        "change_discord_thread_members": prepare_thread_member_undo,
        "undo_discord_thread_member": prepare_thread_member_undo,
        "change_bot_reaction": prepare_control_undo,
        "undo_bot_reaction": prepare_control_undo,
        "change_discord_pin": prepare_control_undo,
        "undo_discord_pin": prepare_control_undo,
        "change_discord_nickname": prepare_nickname_undo,
        "undo_discord_nickname": prepare_nickname_undo,
        "/record add": prepare_record_add_undo,
        "undo_record_add": prepare_record_add_undo,
        "/record edit": prepare_record_edit_undo,
        "undo_record_edit": prepare_record_edit_undo,
        "/raffle prize": prepare_raffle_prize_undo,
        "undo_raffle_prize": prepare_raffle_prize_undo,
        "/account add": prepare_account_add_undo,
        "undo_account_add": prepare_account_add_undo,
        "set_roster_layout": prepare_roster_layout_undo,
        "undo_roster_layout": prepare_roster_layout_undo,
        "manage_role_connection": prepare_role_connection_undo,
        "undo_role_connection": prepare_role_connection_undo,
        "apply_role_connections": prepare_role_connection_scan_undo,
        "undo_role_connection_scan": prepare_role_connection_scan_undo,
        "set_cwl_bonus_scoring": prepare_cwl_bonus_scoring_undo,
        "undo_cwl_bonus_scoring": prepare_cwl_bonus_scoring_undo,
        "set_clan_health_settings": prepare_health_settings_undo,
        "undo_clan_health_settings": prepare_health_settings_undo,
        "set_examiner_profile": prepare_examiner_profile_undo,
        "undo_examiner_profile": prepare_examiner_profile_undo,
    })
    knowledge_store = KnowledgeStore(
        bot.paths.data_root / "agent" / "knowledge",
    )
    await bot.add_cog(
        AgentCog(
            bot,
            account_links=account_links,
            clan_health=clan_health_queries,
            message_search=message_search,
            thread_discovery=thread_discovery,
            roster_queries=rosters.queries,
            cwl_queries=cwl_queries,
            war_queries=war_queries,
            transfer_queries=transfer_queries,
            hibernation_queries=hibernation_queries,
            support_queries=support_queries,
            recruitment_queries=recruitment_queries,
            examination_queries=examination_queries,
            record_queries=record_queries,
            achievement_queries=achievement_queries,
            event_queries=event_queries,
            member_lifecycle_queries=member_lifecycle_queries,
            clan_reporting_queries=clan_reporting_queries,
            role_connection_queries=role_connection_queries,
            knowledge_store=knowledge_store,
            research_jobs=research_jobs,
            research_runner=research_runner,
            action_runner=action_runner,
            action_repository=action_repository,
            transcript_archive=archive,
            persistence=ConversationPersistence(repository),
        )
    )

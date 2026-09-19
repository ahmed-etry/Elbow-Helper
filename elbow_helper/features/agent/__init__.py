"""Core-only mention-driven agent feature."""

from __future__ import annotations
import asyncio

from elbow_helper.discord.message_search import DiscordMessageSearch
from elbow_helper.discord.thread_discovery import DiscordThreadDiscovery
from elbow_helper.configuration.guild import GUILD_ID

from .cog import CoreAgent
from .conversation.transcripts import TranscriptArchive
from .conversation.repository import ConversationRepository
from .conversation.persistence import ConversationPersistence
from .research.repository import ResearchJobRepository
from .research.runner import ResearchJobRunner
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
            "Core agent requires AccountLinks, ClanHealth, Wars, Rosters, CWL, "
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
    message_search = DiscordMessageSearch(bot.http)
    thread_discovery = DiscordThreadDiscovery()
    research_runner = ResearchJobRunner(
        bot=bot, repository=research_jobs, message_search=message_search,
        guild_id=GUILD_ID,
    )
    knowledge_store = KnowledgeStore(
        bot.paths.data_root / "agent" / "knowledge",
    )
    await bot.add_cog(
        CoreAgent(
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
            transcript_archive=archive,
            persistence=ConversationPersistence(repository),
        )
    )

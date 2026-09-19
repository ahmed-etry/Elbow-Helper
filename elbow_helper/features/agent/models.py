"""Internal models for the read-only Core agent."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING
from typing import Mapping

import discord
from discord.ext import commands

from elbow_helper.discord.message_search import DiscordMessageSearch
from elbow_helper.infrastructure.ai import AgentToolDefinition
from .conversation.state import ConversationCheckpoint, ConversationTurn
from .reports.base import ReportArtifact
from .conversation.instructions import TaskInstruction, WorkingState

if TYPE_CHECKING:
    from .research.repository import ResearchJobRepository
    from .knowledge.store import KnowledgeStore
    from elbow_helper.features.clan_health.queries import ClanHealthQueries
    from elbow_helper.features.clan_reporting.queries import ClanReportingQueries
    from elbow_helper.features.achievements.queries import AchievementQueries
    from elbow_helper.features.clan_transfers.queries import ClanTransferQueries
    from elbow_helper.features.hibernation.queries import HibernationQueries
    from elbow_helper.features.support_tickets.queries import SupportTicketQueries
    from elbow_helper.features.recruitment.queries import RecruitmentQueries
    from elbow_helper.features.examination.queries import ExaminationQueries
    from elbow_helper.features.event_stats.queries import EventStatsQueries
    from elbow_helper.features.member_lifecycle.queries import MemberLifecycleQueries
    from elbow_helper.features.records.queries import LeadershipRecordQueries
    from elbow_helper.features.role_connections.queries import RoleConnectionQueries
    from elbow_helper.features.cwl.queries import CwlQueries
    from elbow_helper.features.rosters.services.queries import RosterQueries
    from elbow_helper.features.wars.queries import WarQueries


@dataclass(frozen=True, slots=True)
class AgentAttachment:
    filename: str
    data: bytes
    report_id: str | None = None


@dataclass(slots=True)
class AgentDelivery:
    """Confirmed Discord delivery, separate from generated model output."""

    message_ids: list[int] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    attempted_nonces: list[int] = field(default_factory=list)
    complete: bool = False
    unknown: bool = False
    uncertain_nonce: int | None = None

    def attempt(self, nonce: int) -> None:
        self.attempted_nonces.append(nonce)

    def mark_unknown(self, nonce: int) -> None:
        self.unknown = True
        self.uncertain_nonce = nonce

    def reconcile(self, nonce: int) -> None:
        if self.uncertain_nonce == nonce:
            self.unknown = False
            self.uncertain_nonce = None

    def record(self, message_id: int, text: str) -> None:
        self.message_ids.append(message_id)
        self.text_parts.append(text)


@dataclass(slots=True)
class AgentTurnState:
    """Evidence provenance and reports retained independently of model text."""

    source_channels: set[int] = field(default_factory=set)
    evidence: list[str] = field(default_factory=list)
    reports: dict[str, ReportArtifact] = field(default_factory=dict)
    report_sources: dict[str, frozenset[int]] = field(default_factory=dict)
    report_access_requirements: dict[str, frozenset[str]] = field(default_factory=dict)
    preserved_reports: dict[str, ReportArtifact] = field(default_factory=dict)
    preserved_report_sources: dict[str, frozenset[int]] = field(default_factory=dict)
    preserved_report_access_requirements: dict[str, frozenset[str]] = field(default_factory=dict)
    required_access: set[str] = field(default_factory=set)
    attachments: list[AgentAttachment] = field(default_factory=list)
    history_status: dict[str, int | bool] = field(default_factory=dict)
    authorized_history: tuple[ConversationTurn, ...] | None = None
    authorized_checkpoint: ConversationCheckpoint | None = None
    working: WorkingState = field(default_factory=WorkingState)
    authorized_instructions: tuple[TaskInstruction, ...] = ()
    request_text: str = ""
    stale_knowledge_report_ids: set[str] = field(default_factory=set)
    stale_knowledge_refs: set[tuple[str, str]] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class AgentRequestContext:
    """Trusted runtime objects available to bounded agent tools."""

    bot: commands.Bot
    guild: discord.Guild
    member: discord.Member
    source_message: discord.Message
    account_links: Any
    clan_health: ClanHealthQueries
    message_search: DiscordMessageSearch
    thread_discovery: Any = None
    state: AgentTurnState = field(default_factory=AgentTurnState)
    history: tuple[ConversationTurn, ...] = ()
    roster_queries: RosterQueries | None = None
    cwl_queries: CwlQueries | None = None
    war_queries: WarQueries | None = None
    transfer_queries: ClanTransferQueries | None = None
    hibernation_queries: HibernationQueries | None = None
    support_queries: SupportTicketQueries | None = None
    recruitment_queries: RecruitmentQueries | None = None
    examination_queries: ExaminationQueries | None = None
    record_queries: LeadershipRecordQueries | None = None
    achievement_queries: AchievementQueries | None = None
    event_queries: EventStatsQueries | None = None
    member_lifecycle_queries: MemberLifecycleQueries | None = None
    clan_reporting_queries: ClanReportingQueries | None = None
    role_connection_queries: RoleConnectionQueries | None = None
    knowledge_store: KnowledgeStore | None = None
    research_jobs: ResearchJobRepository | None = None
    conversation_root_id: int | None = None
    attachment_sources: tuple[discord.Message, ...] = ()


AgentToolHandler = Callable[
    [AgentRequestContext, Mapping[str, Any]],
    Awaitable[Mapping[str, Any]],
]


@dataclass(frozen=True, slots=True)
class RegisteredAgentTool:
    """A model-facing definition paired with one trusted handler."""

    definition: AgentToolDefinition
    handler: AgentToolHandler

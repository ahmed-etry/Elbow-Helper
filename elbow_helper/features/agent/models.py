"""Internal models for the agent."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TYPE_CHECKING
from typing import Mapping

import asyncio

import discord
from discord.ext import commands

from elbow_helper.discord.message_search import DiscordMessageSearch
from elbow_helper.infrastructure.ai import AgentToolDefinition
from .conversation.state import ConversationCheckpoint, ConversationTurn
from .reports.base import ReportArtifact
from .conversation.instructions import TaskInstruction, WorkingState
from .actions.contracts import ActionClass
from .datasets.clash_cache import RequestClashReads

if TYPE_CHECKING:
    from .engine.capability_contract import CapabilityContract
    from .research.repository import ResearchJobRepository
    from .actions.store import AgentActionRepository
    from .actions.runner import AgentActionRunner
    from .knowledge.store import KnowledgeStore
    from elbow_helper.features.clan_reporting.queries import ClanReportingQueries
    from elbow_helper.features.achievements.queries import AchievementQueries
    from elbow_helper.features.clan_transfers.queries import ClanTransferQueries
    from elbow_helper.features.hibernation.queries import HibernationQueries
    from elbow_helper.features.support_tickets.queries import SupportTicketQueries
    from elbow_helper.features.recruitment.queries import RecruitmentQueries
    from elbow_helper.features.examination.queries import ExaminationQueries
    from elbow_helper.features.event_stats.queries import EventStatsQueries
    from elbow_helper.features.member_lifecycle.queries import MemberLifecycleQueries
    from elbow_helper.features.ranked_stats.queries import RankedQueries
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
    google_link: str | None = None
    google_warning: str | None = None
    spreadsheet_title: str = ""
    publication_attempted: bool = False
    total_rows: int | None = None
    sheet_rows: tuple[tuple[str, int], ...] = ()


@dataclass(slots=True)
class AgentDelivery:
    """Confirmed Discord delivery, separate from generated model output."""

    message_ids: list[int] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    attempted_nonces: list[int] = field(default_factory=list)
    complete: bool = False
    unknown: bool = False
    uncertain_nonce: int | None = None
    generated_parts: list[str] = field(default_factory=list)

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
class ChannelReactions:
    """Successful bot reactions shared across members and conversations."""

    emojis: tuple[str, ...] | None = ()
    message_id: int = 0
    combo_message_id: int = 0
    combo_revision: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class RequestReactions:
    count: int = 0
    emojis: list[str] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class DirectMessageDelivery:
    sent: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class RequestReplyDelivery:
    delivery: AgentDelivery = field(default_factory=AgentDelivery)
    posts: list[Mapping[str, Any]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class ScopeAudit:
    excluded: dict[str, str] = field(default_factory=dict)
    private_candidates: set[str] = field(default_factory=set)
    offered: bool = False


@dataclass(frozen=True, slots=True)
class EvidenceScope:
    destination_id: int
    destination_levels: frozenset[str]
    levels: frozenset[str]
    requester: bool = False


@dataclass(slots=True)
class AgentTurnState:
    """Evidence provenance and reports retained independently of model text."""

    reactions: RequestReactions = field(default_factory=RequestReactions)
    channel_reactions: ChannelReactions = field(default_factory=ChannelReactions)
    direct_messages: DirectMessageDelivery = field(default_factory=DirectMessageDelivery)
    reply_delivery: RequestReplyDelivery = field(default_factory=RequestReplyDelivery)
    source_channels: set[int] = field(default_factory=set)
    source_access: dict[int, frozenset[str]] = field(default_factory=dict)
    scope: EvidenceScope | None = None
    scope_audit: ScopeAudit = field(default_factory=ScopeAudit)
    private_answer_run: Callable[[], Awaitable[Any]] | None = None
    evidence: list[str] = field(default_factory=list)
    grounding_evidence: dict[str, Any] = field(default_factory=dict)
    local_messages: list[dict[str, Any]] = field(default_factory=list)
    reply_context: dict[str, Any] = field(default_factory=dict)
    channel_bot_gifs: tuple[bool, ...] | None = None
    channel_bot_combos: tuple[bool, ...] | None = None
    reaction_combo_due: bool = False
    reaction_combo_revision: int = 0
    channel_gif_urls: set[str] = field(default_factory=set)
    reports: dict[str, ReportArtifact] = field(default_factory=dict)
    report_sources: dict[str, frozenset[int]] = field(default_factory=dict)
    report_access_requirements: dict[str, frozenset[str]] = field(default_factory=dict)
    preserved_reports: dict[str, ReportArtifact] = field(default_factory=dict)
    preserved_report_sources: dict[str, frozenset[int]] = field(default_factory=dict)
    preserved_report_access_requirements: dict[str, frozenset[str]] = field(default_factory=dict)
    required_access: set[str] = field(default_factory=set)
    help_requester_access: set[str] = field(default_factory=set)
    knowledge_channel_access: set[str] = field(default_factory=set)
    knowledge_core_available: bool = False
    channel_directory: str = ""
    history_cursors: dict[str, dict[str, Any]] = field(default_factory=dict)
    knowledge_title_access: set[str] = field(default_factory=set)
    knowledge_sources: set[str] = field(default_factory=set)
    attachments: list[AgentAttachment] = field(default_factory=list)
    history_status: dict[str, int | bool] = field(default_factory=dict)
    authorized_history: tuple[ConversationTurn, ...] | None = None
    authorized_checkpoint: ConversationCheckpoint | None = None
    working: WorkingState = field(default_factory=WorkingState)
    authorized_instructions: tuple[TaskInstruction, ...] = ()
    request_text: str = ""
    resolved_members: dict[int, str] = field(default_factory=dict)
    member_self_target: bool = False
    image_sources: list[tuple[int, int]] = field(default_factory=list)
    image_source_status: list[str] = field(default_factory=list)
    gif_url: str | None = None
    gif_item_id: str | None = None
    gif_skipped_recent: list[str] = field(default_factory=list)
    gif_requested: bool = False
    gif_attachment: AgentAttachment | None = None
    gif_fallback_text: str = ""
    reply_to: int | None = None
    ping_author: bool = False
    used_gif_urls: set[str] = field(default_factory=set)
    outcomes: list[Any] = field(default_factory=list)
    has_read_answer: bool = False
    provider_failed: bool = False
    member_limit_refused: bool = False
    terminal_cause: str | None = None
    family_catalogue: bool = False
    proposed_changes: list[Any] = field(default_factory=list)
    preview_reply: str | None = None
    preview_first: bool = False


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    display_name: str
    member_id: int


@dataclass(frozen=True, slots=True)
class AgentRequestContext:
    """Trusted runtime objects available to bounded agent tools."""

    bot: commands.Bot
    guild: discord.Guild
    member: discord.Member
    source_message: discord.Message
    account_links: Any
    message_search: DiscordMessageSearch
    thread_discovery: Any = None
    state: AgentTurnState = field(default_factory=AgentTurnState)
    delivery_channel: Any = None
    send_reply: Callable[["AgentRequestContext", str], Awaitable[Mapping[str, Any]]] | None = None
    stop_typing: Callable[[], Awaitable[None]] | None = None
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
    ranked_queries: RankedQueries | None = None
    clan_reporting_queries: ClanReportingQueries | None = None
    role_connection_queries: RoleConnectionQueries | None = None
    knowledge_store: KnowledgeStore | None = None
    research_jobs: ResearchJobRepository | None = None
    action_repository: AgentActionRepository | None = None
    meme_usage_repository: AgentActionRepository | None = None
    action_runner: AgentActionRunner | None = None
    conversation_root_id: int | None = None
    attachment_sources: tuple[discord.Message, ...] = ()
    deadline_monotonic: float | None = None
    dm_delivery: bool = False
    private_pass: bool = False
    # Thread memberships and derived permission audiences; cleared on recheck.
    disclosure_thread_members: dict[Any, Any] = field(default_factory=dict)
    application_owner: AgentIdentity | None = None
    transcript_archive: Any = None
    operator_debug: Any = None
    clash_reads: RequestClashReads = field(default_factory=RequestClashReads, compare=False)


AgentToolHandler = Callable[
    [AgentRequestContext, Mapping[str, Any]],
    Awaitable[Mapping[str, Any]],
]


class AgentCapabilityEffect(StrEnum):
    READ = "read"
    STATE = "conversation_state"
    ARTIFACT = "artifact"
    COMMAND = "command"


@dataclass(frozen=True, slots=True)
class RegisteredAgentTool:
    """A model-facing definition paired with one trusted handler."""

    definition: AgentToolDefinition
    handler: AgentToolHandler
    effect: AgentCapabilityEffect = AgentCapabilityEffect.READ
    action_class: ActionClass = ActionClass.READ
    contract: CapabilityContract | None = None
    returns: str | None = None
    default_visible: bool = True
    specialist_group: str = "records"

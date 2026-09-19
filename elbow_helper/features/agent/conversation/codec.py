"""Explicit JSON conversation format; never persist SDK sessions or reasoning."""

from dataclasses import asdict, fields
from datetime import datetime
import json
import math
import time
from typing import Any

from ..reports.base import retain_report
from ..access import KNOWN_ACCESS_REQUIREMENTS
from .state import (
    CONVERSATION_IDLE_SECONDS, MAX_RETAINED_BYTES, MAX_RETAINED_TURNS, MAX_REPLY_REFERENCES,
    Conversation, ConversationCheckpoint, ConversationRecord, ConversationTurn,
    checkpoint_input_hash,
)
from ..reports.roles import RoleAccountReport
from ..reports.achievement import (
    AchievementLeaderboardReport, AchievementProgressReport,
    CoinTransactionReport, RaffleReport,
)
from ..reports.event import EventScheduleReport
from ..reports.member_lifecycle import MemberLifecycleReport
from ..reports.clan_reporting import MissingElderReport
from ..reports.knowledge import KnowledgeReport
from ..reports.roster import RosterReport
from ..reports.cwl import CwlAssScopeReport, CwlPerformanceReport
from ..reports.cwl_bonus import CwlBonusScopeReport
from ..reports.clan_health import ClanHealthReport
from ..reports.war import RegularWarReport
from ..reports.historical_war import HistoricalRegularWarReport
from ..reports.movement import FamilyMovementReport
from ..reports.transfer import TransferQueueReport
from ..reports.hibernation import HibernationReport
from ..reports.support import SupportTicketReport
from ..reports.recruitment import RecruitmentTrialReport
from ..reports.examination import ExaminationCaseReport
from ..reports.leadership_record import LeadershipRecordReport
from ..files.contracts import (
    CsvImportArtifact,
    TextImportArtifact,
    XlsxImportArtifact,
)
from ..reports.research import DiscordResearchReport
from ..reports.codec import decode_report
from .repository import MAX_SNAPSHOT_BYTES, StoredConversation
from .instructions import WorkingState, TaskInstruction, MAX_ACTIVE_INSTRUCTIONS, MAX_INSTRUCTION_REVISIONS, MAX_INSTRUCTION_BYTES
from .context import build_history_checkpoint


def _integer(value: Any, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("Invalid integer in conversation snapshot")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field in conversation snapshot")
        result[key] = value
    return result


def _record_data(record: ConversationRecord) -> dict[str, Any]:
    return {field.name: getattr(record, field.name) for field in fields(record) if field.init}


def _validate_checkpoint(
    checkpoint: ConversationCheckpoint, turns: list[ConversationTurn],
) -> None:
    covered = tuple(turns[:checkpoint.covered_turn_count])
    if (
        checkpoint.covered_turn_count > len(turns)
        or checkpoint_input_hash(covered) != checkpoint.input_hash
        or checkpoint != build_history_checkpoint(
            turns, covered_turn_count=checkpoint.covered_turn_count,
            created_at=datetime.fromisoformat(checkpoint.created_at),
        )
    ):
        raise ValueError("Conversation checkpoint does not match retained turns")


def encode_conversation(conversation: Conversation, *, root_message_id: int, revision: int,
                        wall_time: float | None = None, monotonic_time: float | None = None) -> StoredConversation:
    now = time.time() if wall_time is None else wall_time
    monotonic = time.monotonic() if monotonic_time is None else monotonic_time
    touched_at = now - max(0, monotonic - conversation.touched_at)
    if any(
        not turn.required_access <= KNOWN_ACCESS_REQUIREMENTS
        for turn in conversation.turns
    ):
        raise ValueError("Conversation turn access requirements are invalid")
    if conversation.checkpoint is not None:
        _validate_checkpoint(conversation.checkpoint, conversation.turns)
    report_ids = set(conversation.reports)
    if (set(conversation.report_sources) != report_ids
            or set(conversation.report_access_requirements) != report_ids):
        raise ValueError("Conversation report provenance is incomplete")
    for report_id in report_ids:
        sources = conversation.report_sources[report_id]
        requirements = conversation.report_access_requirements[report_id]
        if (not sources or any(type(value) is not int or value < 1 for value in sources)
                or not requirements <= KNOWN_ACCESS_REQUIREMENTS):
            raise ValueError("Conversation report provenance is invalid")
        report = conversation.reports[report_id]
        if (
            isinstance(report, MemberLifecycleReport)
            and not report.source_channels <= sources
        ):
            raise ValueError("Member lifecycle report provenance is incomplete")
        if (
            isinstance(report, MissingElderReport)
            and not report.source_channels <= sources
        ):
            raise ValueError("Missing Elder report provenance is incomplete")
        if (
            isinstance(report, KnowledgeReport)
            and not report.required_access <= requirements
        ):
            raise ValueError("Approved knowledge report access is incomplete")
        if (
            isinstance(report, DiscordResearchReport)
            and report.source_channel_id not in sources
        ):
            raise ValueError("Discord research report provenance is incomplete")
        if (
            isinstance(report, (
                CsvImportArtifact, TextImportArtifact, XlsxImportArtifact,
            ))
            and report.channel_id not in sources
        ):
            raise ValueError("Attachment report provenance is incomplete")
    reports = []
    for report in conversation.reports.values():
        if isinstance(report, RoleAccountReport):
            reports.append({"kind": "role_accounts", "report_id": report.report_id,
                            "created_at": report.created_at, "roles": report.roles,
                            "members": report.members,
                            "parent_report_id": report.parent_report_id,
                            "refresh_attempted_player_tags": (
                            report.refresh_attempted_player_tags
                            )})
        elif isinstance(report, AchievementProgressReport):
            reports.append({
                "kind": "achievement_progress", **report.storage_payload(),
            })
        elif isinstance(report, AchievementLeaderboardReport):
            reports.append({
                "kind": "achievement_leaderboard", **report.storage_payload(),
            })
        elif isinstance(report, CoinTransactionReport):
            reports.append({
                "kind": "coin_transactions", **report.storage_payload(),
            })
        elif isinstance(report, RaffleReport):
            reports.append({
                "kind": "raffle", **report.storage_payload(),
            })
        elif isinstance(report, EventScheduleReport):
            reports.append({
                "kind": "event_schedule", **report.storage_payload(),
            })
        elif isinstance(report, MemberLifecycleReport):
            reports.append({
                "kind": "member_lifecycle", **report.storage_payload(),
            })
        elif isinstance(report, MissingElderReport):
            reports.append({
                "kind": "missing_elder", **report.storage_payload(),
            })
        elif isinstance(report, KnowledgeReport):
            reports.append({
                "kind": "approved_knowledge", **report.storage_payload(),
            })
        elif isinstance(report, RosterReport):
            reports.append({"kind": "roster_signups", "report_id": report.report_id, "snapshot": asdict(report.snapshot)})
        elif isinstance(report, CwlPerformanceReport):
            reports.append({"kind": "cwl_performance", "report_id": report.report_id,
                            "guild_id": report.guild_id, "snapshot": asdict(report.snapshot)})
        elif isinstance(report, CwlAssScopeReport):
            reports.append({
                "kind": "cwl_ass_scope", **report.storage_payload(),
            })
        elif isinstance(report, CwlBonusScopeReport):
            reports.append({
                "kind": "cwl_bonus_scope", **report.storage_payload(),
            })
        elif isinstance(report, ClanHealthReport):
            reports.append({"kind": "clan_health", **report.storage_payload()})
        elif isinstance(report, RegularWarReport):
            reports.append({"kind": "regular_war", **report.storage_payload()})
        elif isinstance(report, HistoricalRegularWarReport):
            reports.append({"kind": "historical_regular_wars", **report.storage_payload()})
        elif isinstance(report, FamilyMovementReport):
            reports.append({"kind": "family_account_movements", **report.storage_payload()})
        elif isinstance(report, TransferQueueReport):
            reports.append({"kind": "pending_transfer_requests", **report.storage_payload()})
        elif isinstance(report, HibernationReport):
            reports.append({"kind": "active_hibernation", **report.storage_payload()})
        elif isinstance(report, SupportTicketReport):
            reports.append({"kind": "support_ticket_inventory", **report.storage_payload()})
        elif isinstance(report, RecruitmentTrialReport):
            reports.append({"kind": "active_recruitment_trials", **report.storage_payload()})
        elif isinstance(report, ExaminationCaseReport):
            reports.append({"kind": "examination_case_status", **report.storage_payload()})
        elif isinstance(report, LeadershipRecordReport):
            reports.append({"kind": "active_leadership_records", **report.storage_payload()})
        elif isinstance(report, CsvImportArtifact):
            reports.append({"kind": "csv_import", **report.storage_payload()})
        elif isinstance(report, XlsxImportArtifact):
            reports.append({"kind": "xlsx_import", **report.storage_payload()})
        elif isinstance(report, TextImportArtifact):
            reports.append({"kind": "text_import", **report.storage_payload()})
        elif isinstance(report, DiscordResearchReport):
            reports.append({"kind": "discord_research", **report.storage_payload()})
        else:
            raise ValueError("Unsupported conversation report kind")
    payload = json.dumps({
        "format": 2, "version": conversation.version, "evicted_turns": conversation.evicted_turns,
        "reply_ids": conversation.reply_ids,
        "turns": [{"text": turn.text, "source_channels": sorted(turn.source_channels),
                   "retention_limited": turn.retention_limited,
                   "required_access": sorted(turn.required_access),
                   "knowledge_refs": [list(value) for value in turn.knowledge_refs],
                   "record": _record_data(turn.record) if turn.record else None} for turn in conversation.turns],
        "reports": reports,
        "report_sources": {
            report_id: sorted(conversation.report_sources[report_id])
            for report_id in conversation.reports
        },
        "report_access_requirements": {
            report_id: sorted(conversation.report_access_requirements[report_id])
            for report_id in conversation.reports
        },
        "working": asdict(conversation.working),
        "checkpoint": (
            {
                **asdict(conversation.checkpoint),
                "source_channels": sorted(
                    conversation.checkpoint.source_channels
                ),
                "required_access": sorted(
                    conversation.checkpoint.required_access
                ),
            }
            if conversation.checkpoint is not None else None
        ),
    }, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Conversation snapshot exceeds its storage budget")
    return StoredConversation(conversation.guild_id, conversation.channel_id, root_message_id, revision,
                              touched_at, touched_at + CONVERSATION_IDLE_SECONDS, payload)


def decode_conversation(snapshot: StoredConversation, *, wall_time: float | None = None,
                        monotonic_time: float | None = None) -> Conversation:
    now = time.time() if wall_time is None else wall_time
    monotonic = time.monotonic() if monotonic_time is None else monotonic_time
    for value in (snapshot.guild_id, snapshot.channel_id, snapshot.root_message_id):
        _integer(value)
    _integer(snapshot.revision, minimum=0)
    if (not all(math.isfinite(value) for value in (snapshot.touched_at, snapshot.expires_at, now, monotonic))
            or not 0 < snapshot.expires_at - snapshot.touched_at <= CONVERSATION_IDLE_SECONDS):
        raise ValueError("Invalid conversation retention timestamps")
    if snapshot.expires_at <= now:
        raise ValueError("Conversation snapshot expired")
    if len(snapshot.payload.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Conversation snapshot exceeds its storage budget")
    data = json.loads(snapshot.payload, object_pairs_hook=_unique_object)
    if not isinstance(data, dict) or type(data.get("format")) is not int or data["format"] not in (1, 2):
        raise ValueError("Unsupported conversation snapshot format")
    snapshot_format = data["format"]
    if snapshot_format == 1 and data.get("reports"):
        raise ValueError("Legacy report provenance cannot be restored safely")
    if len(data["turns"]) > MAX_RETAINED_TURNS or len(data["reply_ids"]) > MAX_REPLY_REFERENCES:
        raise ValueError("Conversation snapshot exceeds record bounds")
    conversation = Conversation(snapshot.guild_id, snapshot.channel_id)
    remaining = min(CONVERSATION_IDLE_SECONDS, snapshot.expires_at - now)
    conversation.touched_at = monotonic - (CONVERSATION_IDLE_SECONDS - remaining)
    conversation.version = _integer(data["version"], minimum=0)
    conversation.evicted_turns = _integer(data["evicted_turns"], minimum=0)
    conversation.reply_ids = [_integer(value) for value in data["reply_ids"]]
    if len(conversation.reply_ids) != len(set(conversation.reply_ids)):
        raise ValueError("Duplicate conversation reply identities")
    for row in data["turns"]:
        if type(row["retention_limited"]) is not bool:
            raise ValueError("Invalid retention flag")
        record = row["record"]
        if record is not None:
            record = dict(record)
            for key in ("evidence", "report_ids", "reply_ids"):
                record[key] = tuple(record[key])
            if "attempted_nonces" in record:
                record["attempted_nonces"] = tuple(record["attempted_nonces"])
            for value in (record["request_message_id"], record["member_id"], *record["reply_ids"]):
                _integer(value)
            if (
                type(record["delivery_complete"]) is not bool
                or type(record.get("delivery_unknown", False)) is not bool
            ):
                raise ValueError("Invalid delivery flag")
            record = ConversationRecord(**record)
        raw_required_access = row.get("required_access", ())
        if (not isinstance(raw_required_access, list if snapshot_format == 2 else (list, tuple))
                or any(not isinstance(value, str) for value in raw_required_access)
                or len(raw_required_access) != len(set(raw_required_access))):
            raise ValueError("Invalid turn access requirements")
        required_access = frozenset(raw_required_access)
        if ((snapshot_format == 2 and "required_access" not in row)
                or not required_access <= KNOWN_ACCESS_REQUIREMENTS):
            raise ValueError("Invalid turn access requirements")
        conversation.turns.append(ConversationTurn(
            row["text"],
            frozenset(_integer(value) for value in row["source_channels"]),
            record,
            bool(row["retention_limited"]),
            required_access,
            _decode_knowledge_refs(row.get("knowledge_refs", [])),
        ))
    if sum(turn.retained_bytes for turn in conversation.turns) > MAX_RETAINED_BYTES:
        raise ValueError("Conversation snapshot exceeds retained text bounds")
    checkpoint_data = data.get("checkpoint")
    if checkpoint_data is not None:
        checkpoint = ConversationCheckpoint(
            covered_turn_count=checkpoint_data["covered_turn_count"],
            covered_request_ids=tuple(checkpoint_data["covered_request_ids"]),
            summary=checkpoint_data["summary"],
            source_channels=frozenset(checkpoint_data["source_channels"]),
            required_access=frozenset(checkpoint_data["required_access"]),
            input_hash=checkpoint_data["input_hash"],
            created_at=checkpoint_data["created_at"],
            format_version=checkpoint_data["format_version"],
        )
        _validate_checkpoint(checkpoint, conversation.turns)
        conversation.checkpoint = checkpoint
    for row in data["reports"]:
        retain_report(
            conversation.reports,
            decode_report(row, guild_id=snapshot.guild_id),
        )
    if len(conversation.reports) != len(data["reports"]):
        raise ValueError("Snapshot reports exceed retention bounds or contain duplicate IDs")
    if snapshot_format == 2:
        source_data = data.get("report_sources")
        requirement_data = data.get("report_access_requirements")
        report_ids = set(conversation.reports)
        if (not isinstance(source_data, dict)
                or not isinstance(requirement_data, dict)
                or set(source_data) != report_ids
                or set(requirement_data) != report_ids):
            raise ValueError("Snapshot report provenance is incomplete")
        for report_id in conversation.reports:
            raw_sources = source_data[report_id]
            raw_requirements = requirement_data[report_id]
            if (not isinstance(raw_sources, list) or not raw_sources
                    or len(raw_sources) != len(set(raw_sources))
                    or not isinstance(raw_requirements, list)
                    or len(raw_requirements) != len(set(raw_requirements))):
                raise ValueError("Invalid snapshot report provenance")
            if (any(not isinstance(value, str) for value in raw_requirements)
                    or any(type(value) is not int for value in raw_sources)):
                raise ValueError("Invalid snapshot report access requirements")
            sources = frozenset(_integer(value) for value in raw_sources)
            requirements = frozenset(raw_requirements)
            if not requirements <= KNOWN_ACCESS_REQUIREMENTS:
                raise ValueError("Invalid snapshot report access requirements")
            conversation.report_sources[report_id] = sources
            conversation.report_access_requirements[report_id] = requirements
            report = conversation.reports[report_id]
            if (
                isinstance(report, MemberLifecycleReport)
                and not report.source_channels <= sources
            ):
                raise ValueError("Member lifecycle report provenance is incomplete")
            if (
                isinstance(report, MissingElderReport)
                and not report.source_channels <= sources
            ):
                raise ValueError("Missing Elder report provenance is incomplete")
            if (
                isinstance(report, KnowledgeReport)
                and not report.required_access <= requirements
            ):
                raise ValueError("Approved knowledge report access is incomplete")
            if (
                isinstance(report, DiscordResearchReport)
                and report.source_channel_id not in sources
            ):
                raise ValueError("Discord research report provenance is incomplete")
            if (
                isinstance(report, (
                    CsvImportArtifact, TextImportArtifact, XlsxImportArtifact,
                ))
                and report.channel_id not in sources
            ):
                raise ValueError("Attachment report provenance is incomplete")
    working = data["working"]
    instructions = tuple(TaskInstruction(**row) for row in working["instructions"])
    if len({row.instruction_id for row in instructions}) != len(instructions):
        raise ValueError("Duplicate task instruction identity")
    for row in instructions:
        for value in (row.member_id, row.source_message_id, row.source_channel_id):
            _integer(value)
        if row.retired_by_message_id is not None:
            _integer(row.retired_by_message_id)
        if not row.quote.strip() or len(row.quote) > 2000 or not row.label.strip() or len(row.label) > 80:
            raise ValueError("Invalid task instruction contents")
    if (len(instructions) > MAX_INSTRUCTION_REVISIONS or sum(row.active for row in instructions) > MAX_ACTIVE_INSTRUCTIONS
            or len(json.dumps(working["instructions"], ensure_ascii=False).encode("utf-8")) > MAX_INSTRUCTION_BYTES):
        raise ValueError("Snapshot task instructions exceed retention bounds")
    conversation.working = WorkingState(instructions, _integer(working["version"], minimum=0))
    return conversation


def _decode_knowledge_refs(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise ValueError("Invalid conversation knowledge references")
    references = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or any(not isinstance(part, str) for part in item)
        ):
            raise ValueError("Invalid conversation knowledge references")
        references.append((item[0], item[1]))
    return tuple(references)

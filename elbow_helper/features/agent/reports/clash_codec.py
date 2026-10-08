"""Reconstruct typed report data within its guild boundary."""
from typing import Any
from elbow_helper.features.clan_reporting.queries import (
    MissingElderRow, MissingElderSnapshot,
)
from elbow_helper.features.cwl.queries import (
    CwlAssScopeRow, CwlAssScopeSnapshot,
    CwlBonusAttackScore, CwlBonusIneligiblePlayer, CwlBonusPlayerSummary,
    CwlBonusScopeSnapshot, CwlBonusSettings,
    CwlClanSeasonSummary,
    CwlPerformanceRow,
    CwlPerformanceSnapshot,
)
from elbow_helper.features.clan_transfers.queries import (
    PendingTransferRequest, TransferQueueSnapshot,
)
from ..capabilities.clan_reporting.report import MissingElderReport
from ..capabilities.cwl.report import CwlAssScopeReport, CwlPerformanceReport
from ..capabilities.cwl.bonus_report import CwlBonusScopeReport
from ..capabilities.clan_transfers.report import TransferQueueReport
from .guild_boundary import require_guild


def decode_missing_elder(row: dict[str, Any], guild_id: int) -> Any:
    require_guild(row, guild_id, "Missing Elder report")
    value = row["snapshot"]
    return MissingElderReport(
        row["report_id"], row["guild_id"],
        tuple(row["source_channel_ids"]),
        tuple(row["omitted_inaccessible_clan_codes"]),
        MissingElderSnapshot(
            observed_at=value["observed_at"],
            selected_clan_codes=tuple(value["selected_clan_codes"]),
            skipped_invalid_row_count=value["skipped_invalid_row_count"],
            rows=tuple(MissingElderRow(**item) for item in value["rows"]),
        ),
    )


def decode_cwl_performance(row: dict[str, Any], guild_id: int) -> Any:
    require_guild(row, guild_id, "CWL performance report")
    value = row["snapshot"]
    return CwlPerformanceReport(
        row["report_id"], row["guild_id"], CwlPerformanceSnapshot(
            value["observed_at"], value["history_limit"],
            tuple(value["seasons"]),
            tuple(CwlClanSeasonSummary(**item)
                  for item in value["clan_seasons"]),
            tuple(CwlPerformanceRow(**item) for item in value["rows"]),
        ),
    )


def decode_cwl_ass_scope(row: dict[str, Any], guild_id: int) -> Any:
    require_guild(row, guild_id, "Scoped CWL ASS report")
    value = row["snapshot"]
    return CwlAssScopeReport(
        row["report_id"], row["guild_id"], CwlAssScopeSnapshot(
            observed_at=value["observed_at"],
            clan_code=value["clan_code"], season=value["season"],
            scope_type=value["scope_type"],
            requested_round=value["requested_round"],
            requested_war_id=value["requested_war_id"],
            resolved_war_ids=tuple(value["resolved_war_ids"]),
            resolved_rounds=tuple(value["resolved_rounds"]),
            completed_wars=value["completed_wars"],
            league=value["league"], profile_key=value["profile_key"],
            profile_label=value["profile_label"],
            difficulty_weight=value["difficulty_weight"],
            missed_mode=value["missed_mode"],
            scoring_status=value["scoring_status"],
            coverage_status=value["coverage_status"],
            rows=tuple(CwlAssScopeRow(**item) for item in value["rows"]),
        ),
    )


def decode_cwl_bonus_scope(row: dict[str, Any], guild_id: int) -> Any:
    require_guild(row, guild_id, "CWL bonus scope report")
    value = row["snapshot"]
    return CwlBonusScopeReport(
        row["report_id"], row["guild_id"], CwlBonusScopeSnapshot(
            observed_at=value["observed_at"],
            clan_code=value["clan_code"], season=value["season"],
            scope_type=value["scope_type"],
            requested_round=value["requested_round"],
            requested_war_tag=value["requested_war_tag"],
            resolved_rounds=tuple(value["resolved_rounds"]),
            resolved_war_tags=tuple(value["resolved_war_tags"]),
            settings=CwlBonusSettings(**value["settings"]),
            summaries=tuple(
                CwlBonusPlayerSummary(**item)
                for item in value["summaries"]
            ),
            ineligible=tuple(
                CwlBonusIneligiblePlayer(**item)
                for item in value["ineligible"]
            ),
            attacks=tuple(
                CwlBonusAttackScore(**item) for item in value["attacks"]
            ),
            warnings=tuple(value["warnings"]),
            coverage_status=value["coverage_status"],
        ),
    )


def decode_pending_transfer_requests(row: dict[str, Any], guild_id: int) -> Any:
    require_guild(row, guild_id, "Pending transfer report")
    return TransferQueueReport(
        row["report_id"], row["guild_id"], row["observed_at"],
        row["request_ttl_hours"], row["registered_queue_count"],
        row["omitted_inaccessible_count"],
        tuple(TransferQueueSnapshot(
            clan_code=item["clan_code"], thread_id=item["thread_id"],
            stored_request_count=item["stored_request_count"],
            expired_stored_count=item["expired_stored_count"],
            pending=tuple(PendingTransferRequest(**request)
                          for request in item["pending"]),
        ) for item in row["queues"]),
    )

"""Reconstruct typed report data within its guild boundary."""

from typing import Any
from elbow_helper.features.clan_reporting.queries import (
    MissingElderRow, MissingElderSnapshot,
)
from elbow_helper.features.cwl.queries import (
    CwlClanSeasonSummary,
    CwlPerformanceRow,
    CwlPerformanceSnapshot,
)
from elbow_helper.features.clan_transfers.queries import (
    PendingTransferRequest, TransferQueueSnapshot,
)
from ..capabilities.clan_reporting.report import MissingElderReport
from ..capabilities.cwl.report import CwlPerformanceReport
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

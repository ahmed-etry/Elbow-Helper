"""Workbook rows for leadership records."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any

from ..domain.types import RECORD_CATEGORIES
from ..domain.types import category_label
from ..domain.types import incident_type_label

UTC = dt_timezone.utc


def _format_date(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return "-"


def _linked_accounts(rows: list[dict[str, Any]]) -> str:
    values: list[str] = []
    for row in rows:
        name = str(row.get("player_name_last_seen") or row.get("player_tag") or "Unknown")
        tag = str(row.get("player_tag") or "")
        primary = " [Primary]" if bool(row.get("is_primary")) else ""
        values.append(f"{name} ({tag}){primary}" if tag else f"{name}{primary}")
    return "\n".join(values) if values else "-"


def member_rows(
    records: list[dict[str, Any]],
    links_by_user: dict[int, list[dict[str, Any]]],
) -> list[list[Any]]:
    headers = [
        "Discord Member", "Linked Accounts", "Total Incidents",
        *(category.label for category in RECORD_CATEGORIES),
        "Latest Incident", "Latest Date",
    ]
    rows: list[list[Any]] = [headers]
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[int(record.get("member_id") or 0)].append(record)
    for member_id, member_records in grouped.items():
        newest = member_records[0]
        category_counts = {
            category.key: sum(
                1 for record in member_records
                if str(record.get("category_key") or "") == category.key
            )
            for category in RECORD_CATEGORIES
        }
        rows.append([
            str(newest.get("member_display") or member_id),
            _linked_accounts(links_by_user.get(member_id, [])),
            len(member_records),
            *(category_counts[category.key] for category in RECORD_CATEGORIES),
            (
                f"{category_label(str(newest.get('category_key') or ''))}: "
                f"{incident_type_label(str(newest.get('incident_type_key') or ''))}"
            ),
            _format_date(newest.get("created_ts")),
        ])
    if len(rows) == 1:
        rows.append(["No records", "", 0, 0, 0, 0, 0, "", ""])
    return rows


def incident_rows(
    records: list[dict[str, Any]],
    links_by_user: dict[int, list[dict[str, Any]]],
) -> list[list[Any]]:
    rows: list[list[Any]] = [[
        "Recorded Date", "Discord Member", "Linked Accounts", "Category",
        "Type", "Details", "Recorded By", "Last Updated",
    ]]
    for record in records:
        member_id = int(record.get("member_id") or 0)
        rows.append([
            _format_date(record.get("created_ts")),
            str(record.get("member_display") or member_id),
            _linked_accounts(links_by_user.get(member_id, [])),
            category_label(str(record.get("category_key") or "")),
            incident_type_label(str(record.get("incident_type_key") or "")),
            str(record.get("note") or ""),
            str(record.get("recorder_display") or "-"),
            _format_date(record.get("updated_ts")),
        ])
    if len(rows) == 1:
        rows.append(["", "No records", "", "", "", "", "", ""])
    return rows


def category_incident_rows(
    records: list[dict[str, Any]],
    links_by_user: dict[int, list[dict[str, Any]]],
    category_key: str,
) -> list[list[Any]]:
    rows: list[list[Any]] = [[
        "Recorded Date", "Discord Member", "Linked Accounts", "Type", "Details",
        "Recorded By", "Last Updated",
    ]]
    for record in records:
        if str(record.get("category_key") or "") != category_key:
            continue
        member_id = int(record.get("member_id") or 0)
        rows.append([
            _format_date(record.get("created_ts")),
            str(record.get("member_display") or member_id),
            _linked_accounts(links_by_user.get(member_id, [])),
            incident_type_label(str(record.get("incident_type_key") or "")),
            str(record.get("note") or ""),
            str(record.get("recorder_display") or "-"),
            _format_date(record.get("updated_ts")),
        ])
    if len(rows) == 1:
        rows.append(["", "No records", "", "", "", "", ""])
    return rows

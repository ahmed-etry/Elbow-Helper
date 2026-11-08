"""Shared saved-report shapes and timestamp validation."""

from dataclasses import asdict
from datetime import datetime


def snapshot_payload(report):
    return {"report_id": report.report_id, "guild_id": report.guild_id,
            "snapshot": asdict(report.snapshot)}


def channel_snapshot_payload(report):
    return {"report_id": report.report_id, "guild_id": report.guild_id,
            "source_channel_id": report.source_channel_id,
            "snapshot": asdict(report.snapshot)}


def aware_timestamp(value, error_message):
    if not isinstance(value, str) or not value:
        raise ValueError(error_message)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(error_message) from error
    if parsed.tzinfo is None:
        raise ValueError(error_message)
    return parsed


def valid_timestamp(value):
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False

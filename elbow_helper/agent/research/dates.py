"""Date bounds shared by single-channel and batch research."""

from datetime import datetime, timezone
from typing import Any

from ..models import AgentRequestContext


def search_date(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid search date")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def message_time(context: AgentRequestContext) -> datetime:
    value = context.source_message.created_at
    if not isinstance(value, datetime):
        raise ValueError("Invalid request time")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

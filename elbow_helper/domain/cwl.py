"""Shared CWL calendar rules."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any


def cwl_season_key(wars: Iterable[Mapping[str, Any]]) -> str:
    """Key a league by its earliest UTC start, estimated from each war's round."""
    starts = []
    for war in wars:
        if not isinstance(war, Mapping):
            continue
        round_number = war.get("cwl_round", war.get("_round"))
        if type(round_number) is not int or not 1 <= round_number <= 7:
            continue
        try:
            if war.get("start_ts"):
                start = datetime.fromtimestamp(float(war["start_ts"]), tz=timezone.utc)
            elif isinstance(war.get("_start_dt"), datetime):
                start = war["_start_dt"]
            else:
                start = datetime.strptime(war["startTime"], "%Y%m%dT%H%M%S.%fZ")
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            starts.append(start.astimezone(timezone.utc) - timedelta(days=round_number - 1))
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
    if not starts:
        return ""
    first = min(starts)
    return first.strftime("%Y-%m") + ("-catchup" if first.day > 10 else "")


def is_cwl_window(reference: datetime | None = None) -> bool:
    """Return whether the UTC date is inside the normal CWL window."""
    current = reference or datetime.now(timezone.utc)
    return 1 <= current.astimezone(timezone.utc).day <= 11

"""Chronological selections from the API's rolling battle log."""

from ..research.history import search_date


def battle_date(row):
    try:
        return search_date(row.get("battleTimestamp"))
    except (ValueError, TypeError, AttributeError):
        return None


def date_range(rows):
    dates = [date for row in rows if (date := battle_date(row)) is not None]
    return {"oldest": min(dates).isoformat() if dates else None,
            "newest": max(dates).isoformat() if dates else None}


def select_battles(payload, *, limit, offset=0, order="newest_first", after=None, before=None):
    rows = payload.get("items", [])
    dated = [(battle_date(row), row) for row in rows]
    known = [(date, row) for date, row in dated if date is not None]
    known.sort(key=lambda pair: pair[0], reverse=order == "newest_first")
    selected = [row for date, row in known
                if (after is None or date >= after) and (before is None or date < before)]
    unknown = [row for date, row in dated if date is None]
    if after is None and before is None:
        selected.extend(unknown)
    returned = selected[offset:offset + limit]
    next_offset = offset + len(returned) if offset + len(returned) < len(selected) else None
    coverage = {
        "scope": "latest_rolling_log", "history_complete": False,
        "order": order, "chronology_complete": not unknown,
        "requested_after": after.isoformat() if after else None,
        "requested_before": before.isoformat() if before else None,
        "before_exclusive": True, "available_rows": len(rows), "matching_rows": len(selected),
        "returned_rows": len(returned), "unknown_timestamp_rows": len(unknown),
        "available_range": date_range(rows), "selected_range": date_range(selected),
        "returned_range": date_range(returned), "offset": offset, "next_offset": next_offset,
        "selection_complete": offset == 0 and next_offset is None and not unknown,
    }
    return {**payload, "items": returned}, coverage

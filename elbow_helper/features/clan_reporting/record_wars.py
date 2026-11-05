"""Monthly reporting's calendar and newest-50-entry war-log policy."""

from datetime import datetime, timezone

SECOND = 1_000_000
LOG_WINDOW = 50
RESULTS = {"win": "wins", "lose": "losses", "tie": "ties"}


def month_bounds(month):
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise ValueError("Expected a month as YYYY-MM") from None
    if start.strftime("%Y-%m") != month:
        raise ValueError("Expected a month as YYYY-MM")
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return int(start.timestamp()) * SECOND, int(end.timestamp()) * SECOND


async def monthly_results(reads, clan, month, as_of=None):
    start, end = month_bounds(month)
    entries, offset = [], 0
    while offset is not None:
        result = await reads.log_entries(clan, as_of=as_of, offset=offset)
        as_of = result["coverage"]["requested"]["as_of"]
        entries.extend(result["rows"])
        offset = result["next_offset"]

    def counts(selected):
        totals = {"wins": 0, "losses": 0, "ties": 0, "unknown_result": 0}
        for entry in selected:
            totals[RESULTS.get(entry["result"], "unknown_result")] += 1
        totals["total"] = totals["wins"] + totals["losses"] + totals["ties"]
        return totals
    recent = [entry for entry in entries[:LOG_WINDOW] if start <= entry["end_at"] < end]
    older = [entry for entry in entries[LOG_WINDOW:] if start <= entry["end_at"] < end]
    coverage = result["coverage"]
    source = coverage["signals"][0]
    coverage["requested"].update(after=start, before=end)
    coverage["read_after_month"] = source["last_good_at"] is not None and source["last_good_at"] >= end
    coverage["complete"] &= coverage["read_after_month"] and not source["log_limits"]["backfill_pending"]
    available = result["status"] == "available"
    return {"clan_tag": clan, "month": month, "after": start, "before": end, "status": result["status"],
            **(counts(recent) if available else dict.fromkeys(("wins", "losses", "ties", "unknown_result", "total"))),
            "entries": recent if available else [], "beyond_log_window": counts(older) if available else None,
            "coverage": coverage}

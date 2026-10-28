"""Select distinct observations without treating failed attempts as evidence."""

import json


def observations(evidence):
    """Yield top-level results, retaining partial successes for coverage checks.

    Failed attempts cannot contradict a later successful read. Partial batches
    with any successful item still qualify and must pass the caller's coverage
    validation. Identical results are one observation, regardless of step ID.
    """
    seen = set()
    for result in evidence.values():
        if not isinstance(result, dict):
            continue
        flags = result.get("flags", {})
        if (result.get("error") or isinstance(flags, dict)
                and flags.get("status") in ("failed", "refused")):
            continue
        if result.get("source") == "clash_api":
            items = result.get("items")
            if isinstance(items, dict):
                columns, rows = items.get("columns"), items.get("rows")
                if (isinstance(columns, list) and "status" in columns
                        and isinstance(rows, list) and rows
                        and all(isinstance(row, list) and len(row) == len(columns) for row in rows)):
                    items = [dict(zip(columns, row)) for row in rows]
            if (isinstance(items, list) and items
                    and all(isinstance(item, dict) and item.get("status") in (
                        "not_found", "unavailable", "private", "maintenance", "failed", "refused"
                    ) for item in items)):
                continue
        identity = json.dumps(result, sort_keys=True, ensure_ascii=False, default=str)
        if identity not in seen:
            seen.add(identity)
            yield result

"""Stored records datasets available to agent queries."""

from elbow_helper.features.records.config import DB_PATH

from ...datasets.sources import DataSource


def records_coverage(connection, bindings, limit):
    """Describe stored coverage independently of SQL projections and row limits."""
    after = bindings.get("records_after_ts")
    before = bindings.get("records_before_ts")
    if any(value is not None and (type(value) is not int or value < 0)
           for value in (after, before)) or (
        after is not None and before is not None and after >= before
    ):
        raise ValueError(
            "Use records_after_ts and records_before_ts as UTC Unix seconds, "
            "with after earlier than before."
        )
    window = (
        "FROM records.leadership_records "
        "WHERE (:after IS NULL OR created_ts >= :after) "
        "AND (:before IS NULL OR created_ts < :before)"
    )
    params = {"after": after, "before": before}
    totals = connection.execute(
        "SELECT COUNT(*) AS record_count, COUNT(DISTINCT member_id) AS member_count "
        + window, params,
    ).fetchone()
    members = connection.execute(
        "SELECT member_id, COUNT(*) AS record_count, "
        "SUM(status = 'active') AS active_record_count, "
        "SUM(status = 'removed') AS removed_record_count "
        + window + " GROUP BY member_id ORDER BY member_id LIMIT :limit",
        {**params, "limit": limit + 1},
    ).fetchall()
    return {
        "window": {"after_ts": after, "before_ts": before},
        "scope": (
            "All stored leadership records in this window, before the SQL query's "
            "member/status filters and LIMIT. Null bounds mean unbounded dates."
        ),
        **dict(totals),
        "members": [dict(row) for row in members[:limit]],
        "members_truncated": len(members) > limit,
        "coverage_complete": False,
        "note": (
            "Leadership records are optional and often empty; most leadership does not "
            "use them yet. Sparse or missing records are not evidence of a member's "
            "conduct, activity or standing, and do not prove that an incident did or "
            "did not happen. Existing entries describe only specific logged cases."
        ),
    }


SOURCES = (
    DataSource(
        "records",
        DB_PATH,
        tables=(
            (
                "leadership_records", "lead_plus",
                "Optional, often-empty case log; read only for records questions or "
                "specific logged cases. Absence is not evidence. "
                "status active/removed (historical). "
                "UTC Unix seconds. For date filters bind :records_after_ts (inclusive), "
                ":records_before_ts (exclusive); omitted bounds are unbounded. "
                "source_coverage.records counts all records/members in that window "
                "before other SQL filters and LIMIT.",
            ),
        ),
        config_constant="elbow_helper.features.records.config.DB_PATH",
        coverage=records_coverage,
    ),
)

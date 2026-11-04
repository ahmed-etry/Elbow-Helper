"""Shared CWL calendar rules.

One league a month, signed up and spun on days 1-11. A league's season key is
the month the collector stores for it (YYYY-MM); a key stored with a suffix,
such as June 2026's second league, is read as stored.
"""

from __future__ import annotations

from datetime import datetime, timezone

# A season key: the league's month, with an optional stored suffix.
SEASON_KEY = r"\d{4}-(0[1-9]|1[0-2])(-[a-z]+)?"


def is_cwl_window(reference: datetime | None = None) -> bool:
    """Return whether the UTC date is inside the normal CWL window."""
    current = reference or datetime.now(timezone.utc)
    return 1 <= current.astimezone(timezone.utc).day <= 11

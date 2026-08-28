"""Shared CWL calendar rules."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone


def is_cwl_window(reference: datetime | None = None) -> bool:
    """Return whether the UTC date is inside the normal CWL window."""
    current = reference or datetime.now(timezone.utc)
    return 1 <= current.astimezone(timezone.utc).day <= 11

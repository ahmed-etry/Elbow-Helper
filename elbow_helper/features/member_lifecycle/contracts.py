"""Collaborator contracts required by member-lifecycle workflows."""

from __future__ import annotations

from typing import Any
from typing import Protocol


class HibernationReader(Protocol):
    """Read the active hibernation record for one Discord member."""

    def get_member(self, member_id: int) -> dict[str, Any] | None:
        ...

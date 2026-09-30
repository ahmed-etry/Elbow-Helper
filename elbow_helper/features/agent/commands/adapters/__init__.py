"""Feature-owned slash command adapters."""

from __future__ import annotations

from .clan_health import health_adapters, run_health_player
from .recruitment import recruitment_adapters, run_opinion
from .records import record_adapters


def enabled_adapters():
    return (*recruitment_adapters(), *health_adapters(), *record_adapters())


__all__ = ["enabled_adapters", "run_health_player", "run_opinion"]


"""Clan-health command mixin composition."""

from __future__ import annotations

from .clan import ClanHealthClanCommandMixin
from .health import ClanHealthRootCommandMixin
from .player import ClanHealthPlayerCommandMixin


class ClanHealthCommandMixin(
    ClanHealthRootCommandMixin,
    ClanHealthClanCommandMixin,
    ClanHealthPlayerCommandMixin,
):
    pass


__all__ = ["ClanHealthCommandMixin"]

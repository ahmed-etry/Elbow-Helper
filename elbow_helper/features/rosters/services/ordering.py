"""Roster member order: Town Hall by default, or the in-game clan list order."""

from __future__ import annotations

from typing import Any

from elbow_helper.configuration.clans import CLANS

from ..config import ROSTER_ORDER_WAR
from ..models import Roster
from ..models import RosterLayout
from ..models import RosterMember


STANDINGS_BATCH = 200


class RosterOrder:
    """Order stored members by the roster's chosen sort.

    Members arrive in Town Hall order. Within equal league and trophies, accounts
    in the roster's own clan follow their in-game clan rank, then the rest keep the
    Town Hall order.
    """

    def __init__(self, ranked_reads: Any = None):
        self._ranked_reads = ranked_reads

    async def apply(
        self,
        members: list[RosterMember],
        layout: RosterLayout,
        roster: Roster,
    ) -> list[RosterMember]:
        if layout.sort_order != ROSTER_ORDER_WAR or not members:
            return members
        clan = CLANS.get(roster.clan_code)
        clan_tag = clan.tag if clan is not None else None
        tags = list(dict.fromkeys(member.player_tag for member in members))
        standings: dict[str, dict[str, int | None]] = {}
        for start in range(0, len(tags), STANDINGS_BATCH):
            standings.update(
                await self._ranked_reads.standings(
                    tags[start:start + STANDINGS_BATCH], clan_tag=clan_tag,
                )
            )

        def key(member: RosterMember) -> tuple[int, int, int, int]:
            standing = standings.get(member.player_tag, {})
            rank = standing.get("clan_rank")
            return (
                -(standing.get("league_id") or 0),
                -(standing.get("trophies") or 0),
                0 if rank else 1,
                rank or 0,
            )

        return sorted(members, key=key)

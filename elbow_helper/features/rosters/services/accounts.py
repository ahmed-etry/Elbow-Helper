"""Roster-facing access to linked Clash accounts."""

from __future__ import annotations

from typing import Any
from typing import Protocol

from ..models import LinkedAccount


class AccountLinkSource(Protocol):
    """The Account Links capabilities required by Rosters."""

    def get_links_for_user(self, discord_user_id: int) -> list[dict[str, Any]]: ...

    def get_link_by_tag(self, player_tag: str) -> dict[str, Any] | None: ...

    def get_player_location(self, player_tag: str) -> dict[str, Any] | None: ...

    def get_clan_badge_url(self, clan_code: str) -> str | None: ...


class RosterAccountDirectory:
    """Translate Account Links records into roster-domain account data."""

    def __init__(self, source: AccountLinkSource):
        self._source = source

    def for_member(self, member_id: int) -> list[LinkedAccount]:
        accounts: list[LinkedAccount] = []
        for link in self._source.get_links_for_user(member_id):
            tag = str(link.get("player_tag") or "")
            live = self._source.get_player_location(tag) or {}
            accounts.append(
                LinkedAccount(
                    player_tag=tag,
                    player_name=str(
                        live.get("player_name")
                        or link.get("player_name_last_seen")
                        or tag
                    ),
                    clan_code=str(
                        live.get("clan_code")
                        or link.get("last_seen_clan_code")
                        or ""
                    ),
                    townhall=int(live.get("townhall") or 0),
                )
            )
        return accounts

    def member_id_for_tag(self, player_tag: str) -> int | None:
        link = self._source.get_link_by_tag(player_tag)
        return int(link["discord_user_id"]) if link is not None else None

    def clan_badge_url(self, clan_code: str) -> str | None:
        return self._source.get_clan_badge_url(clan_code)

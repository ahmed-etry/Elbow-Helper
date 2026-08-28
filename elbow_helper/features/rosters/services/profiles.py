"""Clash API profile enrichment for native roster workflows."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
from typing import Any

from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.infrastructure.clash import ClashClient

from elbow_helper.configuration.clans import CLANS

from ..repository import RosterRepository
from ..models import LinkedAccount
from ..models import Roster
from ..models import RosterMember


LOGGER = logging.getLogger(__name__)
CLAN_CODES_BY_TAG = {clan.tag: code for code, clan in CLANS.items()}
HERO_INITIALS = {
    "Barbarian King": "BK",
    "Archer Queen": "AQ",
    "Grand Warden": "GW",
    "Royal Champion": "RC",
    "Minion Prince": "MP",
    "Battle Machine": "BM",
    "Battle Copter": "BC",
}


def _account_from_payload(account: LinkedAccount, payload: dict[str, Any]) -> LinkedAccount:
    heroes: list[tuple[str, int]] = []
    for hero in payload.get("heroes", []) or []:
        if hero.get("village") not in {None, "home"}:
            continue
        level = hero.get("level")
        if not isinstance(level, int):
            continue
        name = str(hero.get("name") or "Hero")
        initials = HERO_INITIALS.get(name) or "".join(
            part[0].upper() for part in name.split() if part
        )
        heroes.append((initials, level))

    clan = payload.get("clan") or {}
    clan_tag = str(clan.get("tag") or "")
    clan_name = str(clan.get("name") or "")
    clan_label = CLAN_CODES_BY_TAG.get(clan_tag) or clan_name
    return replace(
        account,
        player_name=str(payload.get("name") or account.player_name),
        clan_code=clan_label,
        townhall=int(payload.get("townHallLevel") or account.townhall),
        hero_sum=sum(level for _, level in heroes),
        hero_levels=tuple(heroes),
    )


async def fetch_account_profiles(
    accounts: list[LinkedAccount],
    clash_client: ClashClient,
) -> tuple[dict[str, LinkedAccount], set[str]]:
    """Return current profiles by tag and the tags that could not be checked."""
    unique_accounts = {account.player_tag: account for account in accounts}
    if not unique_accounts:
        return {}, set()
    if not clash_client.configured:
        return {}, set(unique_accounts)

    profiles: dict[str, LinkedAccount] = {}
    failed_tags = set(unique_accounts)
    semaphore = asyncio.Semaphore(5)

    async def load(account: LinkedAccount) -> None:
        async with semaphore:
            response = await clash_client.get(
                f"/players/{encode_clash_tag(account.player_tag)}",
                attempts=1,
                timeout_seconds=12,
            )
        payload = response.payload_object
        if response.status != 200 or payload is None:
            LOGGER.debug(
                "Roster account enrichment failed for %s status=%s error=%s",
                account.player_tag,
                response.status,
                response.error,
            )
            return
        profiles[account.player_tag] = _account_from_payload(account, payload)
        failed_tags.discard(account.player_tag)

    await asyncio.gather(*(load(account) for account in unique_accounts.values()))
    return profiles, failed_tags


async def enrich_accounts(
    accounts: list[LinkedAccount],
    clash_client: ClashClient,
) -> list[LinkedAccount]:
    """Add current Town Hall, clan, and hero data when the Clash API is available."""
    if not accounts:
        return accounts

    profiles, _ = await fetch_account_profiles(accounts, clash_client)
    return [profiles.get(account.player_tag, account) for account in accounts]


class RosterProfileService:
    """Refresh stored roster snapshots from current Clash profiles."""

    def __init__(
        self,
        repository: RosterRepository,
        clash_client: ClashClient,
    ):
        self._repository = repository
        self._clash_client = clash_client

    async def refresh(
        self,
        roster: Roster,
        members: list[RosterMember] | None = None,
    ) -> list[RosterMember]:
        if members is None:
            members = await asyncio.to_thread(
                self._repository.list_members,
                roster.id,
                roster.active_cycle_id,
            )
        if not members:
            return members
        profiles = await enrich_accounts(
            [
                LinkedAccount(
                    player_tag=member.player_tag,
                    player_name=member.player_name,
                    clan_code=member.clan_code,
                    townhall=member.townhall,
                    hero_sum=member.hero_sum,
                )
                for member in members
            ],
            self._clash_client,
        )
        snapshots = {
            profile.player_tag: {
                "player_name": profile.player_name,
                "clan_code": profile.clan_code,
                "townhall": profile.townhall,
                "hero_sum": profile.hero_sum,
            }
            for profile in profiles
        }
        await asyncio.to_thread(
            self._repository.refresh_member_snapshots,
            roster.id,
            roster.active_cycle_id,
            snapshots,
        )
        return await asyncio.to_thread(
            self._repository.list_members,
            roster.id,
            roster.active_cycle_id,
        )

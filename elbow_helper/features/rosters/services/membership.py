"""Roster signup and member-management workflows."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
import re

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.clash import ClashClient

from .accounts import RosterAccountDirectory
from ..repository import RosterRepository
from ..models import LinkedAccount
from ..models import Roster
from .profiles import enrich_accounts
from .roles import RosterRoleSynchronizer


RosterLock = Callable[[int], asyncio.Lock]
PostRefresher = Callable[[Roster], Awaitable[None]]


def account_count(count: int) -> str:
    return f"{count} {'account' if count == 1 else 'accounts'}"


@dataclass(frozen=True, slots=True)
class AccountPickerResult:
    accounts: tuple[LinkedAccount, ...] = ()
    message: str | None = None
    return_to_member_picker: bool = False


@dataclass(frozen=True, slots=True)
class MembershipResult:
    message: str
    changed: bool = False


class RosterMembershipService:
    """Apply roster membership rules independently of Discord interactions."""

    def __init__(
        self,
        repository: RosterRepository,
        accounts: RosterAccountDirectory,
        clash_client: ClashClient,
        roles: RosterRoleSynchronizer,
        lock_for: RosterLock,
        refresh_posts: PostRefresher,
    ):
        self._repository = repository
        self._accounts = accounts
        self._clash_client = clash_client
        self._roles = roles
        self._lock_for = lock_for
        self._refresh_posts = refresh_posts

    async def account_picker(
        self,
        roster_id: int,
        member_id: int,
        *,
        mode: str,
        for_other_member: bool,
    ) -> AccountPickerResult:
        roster = await asyncio.to_thread(self._repository.get_roster, roster_id)
        if roster is None:
            return AccountPickerResult(message="That roster no longer exists.")
        if roster.status != "open":
            return AccountPickerResult(message="This roster is closed.")

        current = await asyncio.to_thread(
            self._repository.list_members,
            roster.id,
            roster.active_cycle_id,
        )
        if mode == "remove":
            accounts = [
                LinkedAccount(
                    player_tag=row.player_tag,
                    player_name=row.player_name,
                    clan_code=row.clan_code,
                    townhall=row.townhall,
                )
                for row in current
                if row.discord_user_id == member_id
            ]
        else:
            accounts = await asyncio.to_thread(self._accounts.for_member, member_id)
            if not accounts:
                message = (
                    "That member has no linked Clash accounts."
                    if for_other_member
                    else "You don't have any linked Clash accounts."
                )
                return AccountPickerResult(
                    message=message,
                    return_to_member_picker=for_other_member,
                )
            if len(current) >= roster.max_members:
                return AccountPickerResult(message="This roster is full.")
            signed = {row.player_tag for row in current}
            accounts = [
                account for account in accounts if account.player_tag not in signed
            ]

        if not accounts:
            if mode == "remove":
                message = "No Clash accounts signed up for this roster."
            elif for_other_member:
                message = (
                    "All of that member's linked Clash accounts are already signed up."
                )
            else:
                message = "All your linked Clash accounts are already signed up."
            return AccountPickerResult(
                message=message,
                return_to_member_picker=for_other_member and mode == "signup",
            )

        if mode == "signup":
            accounts = await enrich_accounts(accounts, self._clash_client)
            unknown_townhalls = any(account.townhall <= 0 for account in accounts)
            if roster.min_townhall is not None and not for_other_member:
                accounts = [
                    account
                    for account in accounts
                    if account.townhall >= roster.min_townhall
                ]
                if not accounts:
                    message = (
                        "Town Hall data isn't available for the linked Clash accounts."
                        if unknown_townhalls
                        else (
                            f"No linked Clash accounts meet the TH{roster.min_townhall} "
                            "minimum for this roster."
                        )
                    )
                    return AccountPickerResult(message=message)

        return AccountPickerResult(accounts=tuple(accounts))

    async def apply_selection(
        self,
        roster_id: int,
        *,
        member_id: int,
        player_tags: list[str],
        mode: str,
        account_snapshots: dict[str, LinkedAccount],
        bypass_min_townhall: bool = False,
    ) -> MembershipResult:
        async with self._lock_for(roster_id):
            roster = await asyncio.to_thread(self._repository.get_roster, roster_id)
            if (
                roster is None
                or roster.status != "open"
                or roster.active_cycle_id is None
            ):
                return MembershipResult("This roster is closed.")

            if mode == "signup":
                result = await self._add_selected(
                    roster,
                    member_id,
                    player_tags,
                    account_snapshots,
                    bypass_min_townhall=bypass_min_townhall,
                )
            else:
                result = await self._remove_selected(roster, member_id, player_tags)

            if result.changed:
                await self._refresh_posts(roster)
            return result

    async def _add_selected(
        self,
        roster: Roster,
        member_id: int,
        player_tags: list[str],
        account_snapshots: dict[str, LinkedAccount],
        *,
        bypass_min_townhall: bool,
    ) -> MembershipResult:
        linked_accounts = await asyncio.to_thread(self._accounts.for_member, member_id)
        by_tag = {account.player_tag: account for account in linked_accounts}
        selected = [
            account_snapshots.get(tag, by_tag[tag])
            for tag in player_tags
            if tag in by_tag
        ]
        if not selected:
            return MembershipResult(
                "Those Clash accounts are no longer linked to this member."
            )

        accounts = [
            {
                "player_tag": account.player_tag,
                "player_name": account.player_name,
                "clan_code": account.clan_code,
                "townhall": account.townhall,
                "hero_sum": account.hero_sum,
            }
            for account in selected
            if (
                bypass_min_townhall
                or roster.min_townhall is None
                or account.townhall >= roster.min_townhall
            )
        ]
        if not accounts:
            unknown_townhalls = any(account.townhall <= 0 for account in selected)
            message = (
                "Town Hall data isn't available for those Clash accounts."
                if unknown_townhalls
                else f"This roster requires TH{roster.min_townhall} or higher."
            )
            return MembershipResult(message)

        added, total = await asyncio.to_thread(
            self._repository.add_members,
            roster.id,
            roster.active_cycle_id,
            member_id,
            accounts,
            roster.max_members,
            None if bypass_min_townhall else roster.min_townhall,
        )
        if added == 0:
            current = await asyncio.to_thread(
                self._repository.list_members,
                roster.id,
                roster.active_cycle_id,
            )
            existing = {row.player_tag for row in current}
            if len(current) >= roster.max_members:
                message = "This roster is full."
            elif any(account["player_tag"] in existing for account in accounts):
                message = "Those accounts are already signed up."
            else:
                message = "No accounts were added."
            return MembershipResult(message)

        role_synced = await self._roles.sync(roster, member_id, should_have=True)
        message = f"Added {account_count(added)} to {roster.name}."
        if total >= roster.max_members:
            message += " The roster is now full."
        if not role_synced:
            message += " The signup was saved, but I couldn't add the signup role."
        return MembershipResult(message, changed=True)

    async def _remove_selected(
        self,
        roster: Roster,
        member_id: int,
        player_tags: list[str],
    ) -> MembershipResult:
        removed = await asyncio.to_thread(
            self._repository.remove_members,
            roster.id,
            roster.active_cycle_id,
            discord_user_id=member_id,
            player_tags=player_tags,
        )
        if removed == 0:
            return MembershipResult("None of those accounts are signed up.")

        still_signed = await asyncio.to_thread(
            self._repository.member_has_signup,
            roster.id,
            roster.active_cycle_id,
            member_id,
        )
        role_synced = True
        if not still_signed:
            role_synced = await self._roles.sync(
                roster,
                member_id,
                should_have=False,
            )
        message = f"Removed {account_count(removed)} from {roster.name}."
        if not still_signed and not role_synced:
            message += " The signup was removed, but I couldn't remove the signup role."
        return MembershipResult(message, changed=True)

    async def bulk_add(self, roster_id: int, raw_tags: str) -> MembershipResult:
        raw_values = [
            value for value in re.split(r"[\s,;]+", raw_tags.strip()) if value
        ]
        normalized: list[str] = []
        invalid = 0
        for value in raw_values:
            tag = normalize_player_tag(value)
            if not tag:
                invalid += 1
            elif tag not in normalized:
                normalized.append(tag)
        if not normalized:
            return MembershipResult("Enter at least one valid player tag.")

        roster = await asyncio.to_thread(self._repository.get_roster, roster_id)
        if roster is None:
            return MembershipResult("That roster no longer exists.")
        if roster.status != "open" or roster.active_cycle_id is None:
            return MembershipResult("This roster is closed.")

        tags_by_member: dict[int, list[str]] = {}
        unlinked = 0
        for tag in normalized:
            member_id = await asyncio.to_thread(
                self._accounts.member_id_for_tag,
                tag,
            )
            if member_id is None:
                unlinked += 1
                continue
            tags_by_member.setdefault(member_id, []).append(tag)

        accounts_by_member: dict[int, list[LinkedAccount]] = {}
        for member_id, tags in tags_by_member.items():
            linked = await asyncio.to_thread(self._accounts.for_member, member_id)
            by_tag = {account.player_tag: account for account in linked}
            accounts_by_member[member_id] = [
                by_tag[tag] for tag in tags if tag in by_tag
            ]
            unlinked += len(tags) - len(accounts_by_member[member_id])
        accounts = await enrich_accounts(
            [account for rows in accounts_by_member.values() for account in rows],
            self._clash_client,
        )
        enriched = {account.player_tag: account for account in accounts}

        added = 0
        already_signed = 0
        role_failures = 0
        total = 0
        async with self._lock_for(roster_id):
            roster = await asyncio.to_thread(self._repository.get_roster, roster_id)
            if (
                roster is None
                or roster.status != "open"
                or roster.active_cycle_id is None
            ):
                return MembershipResult("This roster is closed.")
            current = await asyncio.to_thread(
                self._repository.list_members,
                roster.id,
                roster.active_cycle_id,
            )
            existing = {member.player_tag for member in current}
            for member_id, rows in accounts_by_member.items():
                pending = [
                    account for account in rows if account.player_tag not in existing
                ]
                already_signed += len(rows) - len(pending)
                payload = []
                for original in pending:
                    account = enriched.get(original.player_tag, original)
                    payload.append(
                        {
                            "player_tag": account.player_tag,
                            "player_name": account.player_name,
                            "clan_code": account.clan_code,
                            "townhall": account.townhall,
                            "hero_sum": account.hero_sum,
                        }
                    )
                member_added, total = await asyncio.to_thread(
                    self._repository.add_members,
                    roster.id,
                    roster.active_cycle_id,
                    member_id,
                    payload,
                    roster.max_members,
                    None,
                )
                if member_added:
                    added += member_added
                    existing.update(
                        account["player_tag"] for account in payload[:member_added]
                    )
                    if not await self._roles.sync(
                        roster,
                        member_id,
                        should_have=True,
                    ):
                        role_failures += 1

        if added:
            await self._refresh_posts(roster)
        messages = []
        if added:
            messages.append(f"Added {account_count(added)} to **{roster.name}**.")
        elif not (invalid or unlinked or already_signed):
            messages.append("No accounts were added.")
        if already_signed:
            verb = "is" if already_signed == 1 else "are"
            messages.append(
                f"{account_count(already_signed).capitalize()} "
                f"{verb} already signed up."
            )
        if unlinked:
            unit = "tag wasn't" if unlinked == 1 else "tags weren't"
            messages.append(f"{unlinked} player {unit} linked.")
        if invalid:
            unit = "entry wasn't" if invalid == 1 else "entries weren't"
            messages.append(f"{invalid} {unit} a valid player tag.")
        if total >= roster.max_members:
            messages.append("The roster is full.")
        if role_failures:
            messages.append("Some signup roles couldn't be added.")
        return MembershipResult(
            " ".join(messages),
            changed=bool(added),
        )

    async def remove_players(
        self,
        roster_id: int,
        player_tags: list[str],
    ) -> MembershipResult:
        async with self._lock_for(roster_id):
            roster = await asyncio.to_thread(self._repository.get_roster, roster_id)
            if roster is None or roster.active_cycle_id is None:
                return MembershipResult("That roster no longer exists.")
            members = await asyncio.to_thread(
                self._repository.list_members,
                roster.id,
                roster.active_cycle_id,
            )
            selected = [
                member for member in members if member.player_tag in player_tags
            ]
            by_member: dict[int, list[str]] = {}
            for member in selected:
                by_member.setdefault(member.discord_user_id, []).append(
                    member.player_tag
                )
            removed = 0
            for member_id, tags in by_member.items():
                removed += await asyncio.to_thread(
                    self._repository.remove_members,
                    roster.id,
                    roster.active_cycle_id,
                    discord_user_id=member_id,
                    player_tags=tags,
                )
            if not removed:
                return MembershipResult(
                    "Those accounts are no longer on this roster."
                )

            remaining = await asyncio.to_thread(
                self._repository.list_members,
                roster.id,
                roster.active_cycle_id,
            )
            remaining_member_ids = {
                member.discord_user_id for member in remaining
            }
            for member_id in by_member:
                if member_id not in remaining_member_ids:
                    await self._roles.sync(
                        roster,
                        member_id,
                        should_have=False,
                    )
            await self._refresh_posts(roster)
            return MembershipResult(
                f"Removed {account_count(removed)} from **{roster.name}**.",
                changed=True,
            )

    async def clear(self, roster_id: int) -> MembershipResult:
        async with self._lock_for(roster_id):
            roster = await asyncio.to_thread(self._repository.get_roster, roster_id)
            if roster is None:
                return MembershipResult("That roster no longer exists.")
            member_ids = await asyncio.to_thread(
                self._repository.clear_members,
                roster.id,
                roster.active_cycle_id,
            )
            for member_id in member_ids:
                await self._roles.sync(
                    roster,
                    member_id,
                    should_have=False,
                )
            await self._refresh_posts(roster)
            count = len(member_ids)
            return MembershipResult(
                (
                    f"Cleared current signups for {count} "
                    f"{'member' if count == 1 else 'members'} "
                    f"from **{roster.name}**."
                ),
                changed=bool(count),
            )

    async def remove_ineligible_member(
        self,
        guild_id: int,
        member_id: int,
    ) -> int:
        return await self.reconcile_eligible_members(
            guild_id,
            eligible_member_ids=set(),
            target_member_ids={member_id},
        )

    async def reconcile_eligible_members(
        self,
        guild_id: int,
        eligible_member_ids: set[int],
        *,
        target_member_ids: set[int] | None = None,
    ) -> int:
        rosters = await asyncio.to_thread(self._repository.list_rosters, guild_id)
        removed_total = 0
        for listed_roster in rosters:
            async with self._lock_for(listed_roster.id):
                roster = await asyncio.to_thread(
                    self._repository.get_roster,
                    listed_roster.id,
                )
                if roster is None or roster.active_cycle_id is None:
                    continue
                members = await asyncio.to_thread(
                    self._repository.list_members,
                    roster.id,
                    roster.active_cycle_id,
                )
                ineligible: dict[int, list[str]] = {}
                for row in members:
                    if (
                        target_member_ids is not None
                        and row.discord_user_id not in target_member_ids
                    ):
                        continue
                    if row.discord_user_id in eligible_member_ids:
                        continue
                    ineligible.setdefault(row.discord_user_id, []).append(
                        row.player_tag
                    )
                if not ineligible:
                    continue
                roster_removed = 0
                for member_id, player_tags in ineligible.items():
                    removed = await asyncio.to_thread(
                        self._repository.remove_members,
                        roster.id,
                        roster.active_cycle_id,
                        discord_user_id=member_id,
                        player_tags=player_tags,
                    )
                    if not removed:
                        continue
                    roster_removed += removed
                    await self._roles.sync(roster, member_id, should_have=False)
                if roster_removed:
                    removed_total += roster_removed
                    await self._refresh_posts(roster)
        return removed_total

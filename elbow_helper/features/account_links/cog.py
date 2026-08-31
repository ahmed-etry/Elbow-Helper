"""Clan links source-of-truth and polling cog."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import tasks
from elbow_helper.discord.interactions import deny
from elbow_helper.discord.interactions import fail
from elbow_helper.discord.interactions import warn

from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.clash import ClashClient
from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import CORE
from elbow_helper.configuration.roles import RECRUITERS
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL

from .config import COC_HTTP_TOTAL_TIMEOUT_SECONDS
from .config import CLAN_FETCH_BACKOFF_SECONDS
from .config import CLAN_FETCH_CONCURRENCY
from .config import CLAN_FETCH_RETRIES
from .config import CLAN_FETCH_WARNING_COOLDOWN_SECONDS
from .config import LINK_ROLE_AT_OR_ABOVE_ELDER
from .config import POLL_INTERVAL_MINUTES
from .config import TRACKED_CLAN_CODES
from .database import AccountLinksDbMixin
from .matching import find_best_candidate
from .review import AccountLinksReviewMixin


LOGGER = logging.getLogger(__name__)


class AccountLinks(commands.Cog, AccountLinksDbMixin, AccountLinksReviewMixin):
    """Source-of-truth link storage and clan scan state."""

    account_group = app_commands.Group(name="account", description="Manage linked player accounts")

    def __init__(self, bot: commands.Bot, clash_client: ClashClient):
        self.bot = bot
        self.clash_client = clash_client
        self._board_refresher = None
        self._snapshot_lock = asyncio.Lock()
        self._snapshot_ready = asyncio.Event()
        self._ready_refresh_task: asyncio.Task[None] | None = None
        self._clan_members: dict[str, dict[str, dict[str, Any]]] = {}
        self._clan_badge_urls: dict[str, str] = {}
        self._player_locations: dict[str, dict[str, Any]] = {}
        self._last_snapshot_complete = False
        self._clan_fetch_warning_state: dict[str, dict[str, Any]] = {}
        self._init_db()
        self._poll_clans_loop.start()

    async def cog_load(self) -> None:
        await self.register_pending_suggestion_views()

    def cog_unload(self) -> None:
        if self._poll_clans_loop.is_running():
            self._poll_clans_loop.cancel()
        if self._ready_refresh_task and not self._ready_refresh_task.done():
            self._ready_refresh_task.cancel()

    async def _refresh_after_ready(self) -> None:
        try:
            await self.refresh_now()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Account-link ready refresh failed")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._snapshot_ready.is_set():
            return
        if self._ready_refresh_task and not self._ready_refresh_task.done():
            return
        self._ready_refresh_task = asyncio.create_task(self._refresh_after_ready())

    def _log_clan_fetch_failure(self, key: str, label: str, detail: str, *, transient: bool) -> None:
        now = time.monotonic()
        state = self._clan_fetch_warning_state.get(key)
        if state:
            last_detail = str(state.get("detail") or "")
            last_ts = float(state.get("last_ts") or 0.0)
            if (
                detail == last_detail
                and (now - last_ts) < CLAN_FETCH_WARNING_COOLDOWN_SECONDS
            ):
                state["suppressed"] = int(state.get("suppressed") or 0) + 1
                self._clan_fetch_warning_state[key] = state
                return

        suppressed = int((state or {}).get("suppressed") or 0)
        suffix = f" (suppressed {suppressed} similar warnings)" if suppressed else ""
        log = LOGGER.info if transient else LOGGER.warning
        log("Failed to fetch %s: %s%s", label, detail, suffix)
        self._clan_fetch_warning_state[key] = {
            "detail": detail,
            "last_ts": now,
            "suppressed": 0,
        }

    @staticmethod
    def _group_transient_fetch_warning(key: str, label: str) -> tuple[str, str]:
        category = key.split(":", 1)[0]
        if category == "members":
            return "members:transient", "clan member snapshots"
        return key, label

    async def _fetch_coc_json(
        self,
        path: str,
        *,
        warning_key: str,
        warning_label: str,
        log_failure: bool = True,
    ) -> dict[str, Any] | None:
        response = await self.clash_client.get(
            path,
            attempts=CLAN_FETCH_RETRIES,
            timeout_seconds=COC_HTTP_TOTAL_TIMEOUT_SECONDS,
            backoff_seconds=CLAN_FETCH_BACKOFF_SECONDS,
            maximum_backoff_seconds=10.0,
        )
        payload = response.payload_object
        if response.ok and payload is not None:
            self._clan_fetch_warning_state.pop(warning_key, None)
            transient_key, _ = self._group_transient_fetch_warning(
                warning_key,
                warning_label,
            )
            self._clan_fetch_warning_state.pop(transient_key, None)
            return payload

        detail = str(response.error or f"status={response.status}").strip()
        if response.attempts > 1:
            detail = f"{detail} (after {response.attempts} attempts)"
        if log_failure:
            log_key, log_label = (
                self._group_transient_fetch_warning(warning_key, warning_label)
                if response.transient
                else (warning_key, warning_label)
            )
            self._log_clan_fetch_failure(
                log_key,
                log_label,
                detail,
                transient=response.transient,
            )
        else:
            LOGGER.debug("Failed to fetch %s: %s", warning_label, detail)
        return None

    async def _fetch_clan_members(self, clan_code: str) -> list[dict[str, Any]] | None:
        if not self.clash_client.configured:
            return []
        clan_tag = CLANS[clan_code].tag
        path = f"/clans/{encode_clash_tag(clan_tag)}"
        payload = await self._fetch_coc_json(
            path,
            warning_key=f"members:{clan_code}",
            warning_label=f"clan members for {clan_code}",
        )
        if payload is None:
            return None

        badge_urls = payload.get("badgeUrls") or {}
        badge_url = next(
            (
                badge_urls.get(size)
                for size in ("small", "medium", "large")
                if isinstance(badge_urls.get(size), str) and badge_urls.get(size)
            ),
            None,
        )
        if badge_url:
            self._clan_badge_urls[clan_code] = badge_url

        out: list[dict[str, Any]] = []
        for member in payload.get("memberList", []) or []:
            player_tag = str(member.get("tag") or "").strip().upper()
            if not player_tag:
                continue
            out.append(
                {
                    "player_tag": player_tag,
                    "player_name": str(member.get("name") or player_tag),
                    "clan_code": clan_code,
                    "clan_tag": clan_tag,
                    "townhall": int(member.get("townHallLevel") or 0),
                    "role": str(member.get("role") or ""),
                }
            )
        return out

    def get_clan_badge_url(self, clan_code: str) -> str | None:
        """Return the latest badge URL from the existing clan snapshot."""
        return self._clan_badge_urls.get(clan_code)

    def get_player_location(self, player_tag: str) -> dict[str, Any] | None:
        """Return a copy of a player's latest tracked clan snapshot."""
        location = self._player_locations.get(player_tag)
        return dict(location) if location is not None else None

    async def _candidate_members(self) -> list[discord.Member]:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return []
        if guild.members:
            return [member for member in guild.members if not member.bot]
        try:
            return [member async for member in guild.fetch_members(limit=None) if not member.bot]
        except (discord.Forbidden, discord.HTTPException):
            return []

    async def _rebuild_snapshots(self) -> None:
        fresh_members: dict[str, dict[str, dict[str, Any]]] = {}
        player_locations: dict[str, dict[str, Any]] = {}
        semaphore = asyncio.Semaphore(CLAN_FETCH_CONCURRENCY)

        async def fetch_one(clan_code: str) -> tuple[str, list[dict[str, Any]] | None]:
            async with semaphore:
                return clan_code, await self._fetch_clan_members(clan_code)

        results = await asyncio.gather(*(fetch_one(clan_code) for clan_code in TRACKED_CLAN_CODES))
        self._last_snapshot_complete = self.clash_client.configured and all(
            members is not None for _, members in results
        )
        for clan_code, members in results:
            if members is None:
                clan_map = dict(self._clan_members.get(clan_code, {}))
            else:
                clan_map = {str(row["player_tag"]): row for row in members}
            fresh_members[clan_code] = clan_map
            for row in clan_map.values():
                player_locations[str(row["player_tag"])] = row

        self._clan_members = fresh_members
        self._player_locations = player_locations

    async def _prune_departed_suggestions(self) -> None:
        if not self._last_snapshot_complete:
            return
        current_tags = set(self._player_locations)
        for suggestion in self.list_pending_suggestions():
            player_tag = str(suggestion.get("player_tag") or "")
            if not player_tag or player_tag in current_tags:
                continue
            await self._finalize_suggestion_message(suggestion)
            self.delete_suggestion(player_tag)

    async def _process_unlinked_players(self) -> None:
        links = self.get_all_links()
        members = await self._candidate_members()
        for player_tag, row in self._player_locations.items():
            if self.is_ignored_tag(player_tag):
                continue
            if player_tag in links:
                self.update_link_last_seen(
                    player_tag=player_tag,
                    player_name_last_seen=str(row.get("player_name") or ""),
                    last_seen_clan_tag=str(row.get("clan_tag") or ""),
                    last_seen_clan_code=str(row.get("clan_code") or ""),
                    last_seen_role=str(row.get("role") or ""),
                )
                pending = self.get_pending_suggestion(player_tag)
                if pending:
                    self.delete_suggestion(player_tag)
                    await self._finalize_suggestion_message(pending)
                continue
            if self.get_pending_suggestion(player_tag):
                continue
            candidate = find_best_candidate(player_name=str(row.get("player_name") or ""), members=members)
            await self.publish_suggestion(
                {
                    "player_tag": player_tag,
                    "player_name": str(row.get("player_name") or ""),
                    "current_clan_code": str(row.get("clan_code") or ""),
                    "current_clan_tag": str(row.get("clan_tag") or ""),
                    "proposed_discord_user_id": candidate.member.id if candidate else 0,
                    "proposed_display_name": candidate.member.display_name if candidate else "",
                }
            )

    async def lookup_players(self, player_tags: list[str]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        if not self.clash_client.configured:
            return [{"player_tag": tag, "player_name": tag} for tag in player_tags]
        for player_tag in player_tags:
            path = f"/players/{encode_clash_tag(player_tag)}"
            player_name = player_tag
            payload = await self._fetch_coc_json(
                path,
                warning_key=f"player:{player_tag}",
                warning_label=f"player name for {player_tag}",
                log_failure=False,
            )
            if payload is not None:
                player_name = str(payload.get("name") or player_tag)
            out.append({"player_tag": player_tag, "player_name": player_name})
        return out

    async def refresh_linked_boards(self) -> None:
        if self._board_refresher is not None:
            await self._board_refresher.refresh_all_missing_elder_boards_from_links()

    async def _try_refresh_linked_boards(self) -> None:
        try:
            await self.refresh_linked_boards()
        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
            LOGGER.exception("Failed to refresh Missing Elder Rank boards after account-link changes")

    async def refresh_board_for_clan(self, clan_code: str) -> None:
        if self._board_refresher is not None:
            await self._board_refresher.refresh_missing_elder_board_now(
                clan_code,
                reposition_if_buried=False,
            )

    def set_board_refresher(self, board_refresher) -> None:
        """Supply the clan-board updater after ClanReporting loads."""
        self._board_refresher = board_refresher

    def _can_manage_accounts(self, member: discord.abc.User | discord.Member) -> bool:
        roles = getattr(member, "roles", [])
        return any(getattr(role, "id", None) in (CORE | RECRUITERS) for role in roles)

    def _parse_player_tag_input(self, raw_input: str) -> tuple[list[str], list[str]]:
        if not raw_input:
            return [], []

        normalized = raw_input.replace(",", " ").replace(";", " ").replace("\n", " ")
        values = [value.strip() for value in normalized.split(" ") if value.strip()]

        valid: list[str] = []
        invalid: list[str] = []
        seen: set[str] = set()
        for value in values:
            tag = normalize_player_tag(value)
            if not tag:
                invalid.append(value.strip())
                continue
            if tag in seen:
                continue
            seen.add(tag)
            valid.append(tag)

        return valid, invalid

    @account_group.command(name="add", description="Link one or more Clash accounts to a Discord member.")
    @app_commands.describe(
        member="Discord member who owns these Clash accounts.",
        tags="Clash account tags to link.",
    )
    async def account_add(self, interaction: discord.Interaction, member: discord.Member, tags: str) -> None:
        if not self._can_manage_accounts(interaction.user):
            await deny(interaction)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            valid_tags, invalid_tags = self._parse_player_tag_input(tags)
            if not valid_tags:
                await warn(interaction, "None of those player tags were recognized. Check the tags and try again.")
                return

            player_rows = await self.lookup_players(valid_tags)
            existing_member_links = self.get_links_for_user(member.id)
            existing_links = self.get_all_links()
            has_primary = any(bool(row.get("is_primary")) for row in existing_member_links)
            lines: list[str] = []
            link_rows: list[dict[str, object]] = []

            for row in player_rows:
                tag = str(row["player_tag"])
                existing = existing_links.get(tag)
                is_primary = False
                if existing and int(existing["discord_user_id"]) == member.id:
                    is_primary = bool(existing.get("is_primary"))
                elif not has_primary:
                    is_primary = True
                    has_primary = True

                link_rows.append(
                    {
                        "player_tag": tag,
                        "discord_user_id": member.id,
                        "is_primary": is_primary,
                        "player_name_last_seen": str(row["player_name"]),
                    }
                )

                if existing and int(existing["discord_user_id"]) != member.id:
                    lines.append(
                        f"- Reassigned {row['player_name']} (`{tag}`) from <@{int(existing['discord_user_id'])}> to {member.mention}"
                    )
                else:
                    lines.append(f"- Linked {row['player_name']} (`{tag}`) to {member.mention}")

            self.upsert_links(link_rows)
            await self._try_refresh_linked_boards()

            if invalid_tags:
                lines.append(f"- Invalid player tags: {', '.join(invalid_tags)}")

            await interaction.followup.send("\n".join(lines), ephemeral=True)
        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
            LOGGER.exception("Manual account add failed: member_id=%s", member.id)
            await fail(interaction)

    @account_group.command(name="remove", description="Unlink one or more Clash accounts.")
    @app_commands.describe(tags="Clash account tags to unlink.")
    async def account_remove(self, interaction: discord.Interaction, tags: str) -> None:
        if not self._can_manage_accounts(interaction.user):
            await deny(interaction)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            valid_tags, invalid_tags = self._parse_player_tag_input(tags)
            if not valid_tags:
                await warn(interaction, "None of those player tags were recognized. Check the tags and try again.")
                return

            existing_links = self.get_all_links()
            tags_to_remove: list[str] = []
            lines: list[str] = []
            for tag in valid_tags:
                existing = existing_links.get(tag)
                if not existing:
                    lines.append(f"- No Discord member linked to `{tag}`")
                    continue
                tags_to_remove.append(tag)
                lines.append(f"- Removed `{tag}` from <@{int(existing['discord_user_id'])}>")

            if tags_to_remove:
                self.delete_links(tags_to_remove)
                await self._try_refresh_linked_boards()

            if invalid_tags:
                lines.append(f"- Invalid player tags: {', '.join(invalid_tags)}")

            await interaction.followup.send("\n".join(lines), ephemeral=True)
        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
            LOGGER.exception("Manual account remove failed")
            await fail(interaction)

    @account_group.command(name="list", description="Find who an account is linked to, or see a member's linked accounts.")
    @app_commands.describe(
        member="Discord member whose linked Clash accounts you want to view.",
        tag="Clash account tag whose linked Discord member you want to find.",
    )
    async def account_list(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        tag: str | None = None,
    ) -> None:
        if not self._can_manage_accounts(interaction.user):
            await deny(interaction)
            return

        if (member is None and tag is None) or (member is not None and tag is not None):
            await warn(interaction, "Choose a Discord member or enter a player tag.")
            return

        if member is not None:
            rows = self.get_links_for_user(member.id)
            if not rows:
                await interaction.response.send_message(f"No linked Clash accounts found for {member.mention}.", ephemeral=True)
                return

            lines = [
                f"- {row.get('player_name_last_seen') or row['player_tag']} (`{row['player_tag']}`)"
                for row in rows
            ]
            embed = discord.Embed(
                title=f"Accounts Linked to {member.display_name}",
                description="\n".join(lines),
                color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            )
            embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        normalized_tag = normalize_player_tag(tag or "")
        if not normalized_tag:
            await warn(interaction, "That player tag is invalid.")
            return

        row = self.get_link_by_tag(normalized_tag)
        if not row:
            await interaction.response.send_message(f"No Discord member is linked to `{normalized_tag}`.", ephemeral=True)
            return

        guild = interaction.guild
        member = guild.get_member(int(row["discord_user_id"])) if guild else None
        player_name = str(row.get("player_name_last_seen") or normalized_tag)
        owner_value = member.mention if isinstance(member, discord.Member) else f"<@{int(row['discord_user_id'])}>"
        embed = discord.Embed(
            title=f"Discord Member Linked to {player_name}",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.add_field(name="Player Tag", value=f"`{normalized_tag}`", inline=False)
        embed.add_field(name="Discord Member", value=owner_value, inline=False)
        if row.get("last_seen_clan_code"):
            embed.add_field(name="Most Recent Clan", value=str(row["last_seen_clan_code"]), inline=True)
        if row.get("last_seen_role"):
            embed.add_field(name="Most Recent Clan Role", value=str(row["last_seen_role"]), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def refresh_now(self, *, refresh_boards: bool = True) -> None:
        async with self._snapshot_lock:
            await self._rebuild_snapshots()
            await self._prune_departed_suggestions()
            await self._process_unlinked_players()
            self._snapshot_ready.set()
        if refresh_boards:
            await self.refresh_linked_boards()

    async def wait_until_snapshot_ready(self) -> None:
        await self._snapshot_ready.wait()

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def _poll_clans_loop(self) -> None:
        await self.bot.wait_until_ready()
        await self.refresh_now()

    @_poll_clans_loop.before_loop
    async def _before_poll_clans_loop(self) -> None:
        await self.bot.wait_until_ready()

    def get_missing_elder_rows(self, clan_code: str) -> list[dict[str, Any]]:
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return []
        rows: list[dict[str, Any]] = []
        clan_members = self._clan_members.get(clan_code, {})
        if not clan_members:
            return rows
        for row in clan_members.values():
            player_tag = str(row.get("player_tag") or "")
            link = self.get_link_by_tag(player_tag)
            if not link:
                continue
            member = guild.get_member(int(link["discord_user_id"]))
            if member is None:
                continue
            role_ids = {role.id for role in member.roles}
            from elbow_helper.configuration.roles import ELDER_ROLE_ID, LEAD_PLUS
            if ELDER_ROLE_ID not in role_ids:
                continue
            if role_ids & LEAD_PLUS:
                continue
            ingame_role = str(row.get("role") or "")
            if ingame_role in LINK_ROLE_AT_OR_ABOVE_ELDER:
                continue
            rows.append(
                {
                    "discord_user_id": member.id,
                    "discord_display_name": member.display_name,
                    "player_tag": player_tag,
                    "player_name": str(row.get("player_name") or player_tag),
                    "clan_code": clan_code,
                    "ingame_role": ingame_role,
                }
            )
        rows.sort(key=lambda item: (str(item["discord_display_name"]).casefold(), str(item["player_name"]).casefold()))
        return rows

"""CWL transfer reminder workflows backed by native rosters."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime
from datetime import timedelta
from datetime import timezone as dt_timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set

import discord
from discord import app_commands
from elbow_helper.discord.interactions import deny
from elbow_helper.features.rosters.config import CWL_CLAN_ROSTER_IDS
from elbow_helper.features.rosters.models import LinkedAccount
from elbow_helper.features.rosters.models import RosterMember
from elbow_helper.features.rosters.services.profiles import fetch_account_profiles

from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.configuration.channels import CLAN_TRANSFERS
from elbow_helper.configuration.clans import CLAN_NAMES
from elbow_helper.configuration.roles import CWL_HELPERS
from elbow_helper.configuration.roles import LEAD_PLUS
from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic
from .config import CLAN_LINKS
from .config import CWL_CLAN_CODES
from .config import CWL_CLAN_TAGS
from .config import TRANSFER_STATE_FILE
from .config import TRANSFER_REMINDER_RETENTION_HOURS


LOGGER = logging.getLogger(__name__)


class CwlTransferMixin:
    def _build_transfer_reminder_delete_at(self, now: Optional[datetime] = None) -> str:
        base = now or datetime.now(dt_timezone.utc)
        return (base + timedelta(hours=TRANSFER_REMINDER_RETENTION_HOURS)).isoformat()


    def _parse_transfer_reminder_delete_at(self, raw: Any) -> Optional[datetime]:
        if not isinstance(raw, str):
            return None
        try:
            delete_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if delete_at.tzinfo is None:
            return delete_at.replace(tzinfo=dt_timezone.utc)
        return delete_at.astimezone(dt_timezone.utc)


    def _normalize_transfer_reminder_messages(self, raw_entries: Any) -> List[Dict[str, Any]]:
        reminders: List[Dict[str, Any]] = []
        changed = False
        if not isinstance(raw_entries, list):
            return reminders
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            channel_id_raw = entry.get("channel_id")
            message_id_raw = entry.get("message_id")
            try:
                channel_id = int(channel_id_raw)
                message_id = int(message_id_raw)
            except (TypeError, ValueError):
                continue
            if (
                isinstance(channel_id, int)
                and not isinstance(channel_id, bool)
                and channel_id > 0
                and isinstance(message_id, int)
                and not isinstance(message_id, bool)
                and message_id > 0
            ):
                delete_at = self._parse_transfer_reminder_delete_at(entry.get("delete_at"))
                normalized_delete_at = (
                    delete_at.isoformat()
                    if delete_at is not None
                    else self._build_transfer_reminder_delete_at()
                )
                if delete_at is None or entry.get("delete_at") != normalized_delete_at:
                    changed = True
                reminders.append(
                    {
                        "channel_id": channel_id,
                        "message_id": message_id,
                        "delete_at": normalized_delete_at,
                    }
                )
        if changed:
            self._transfer_state_needs_save = True
        return reminders


    def _load_transfer_state(self) -> Dict[str, Any]:
        # Load the reminder messages that may need replacing or expiring.
        try:
            if TRANSFER_STATE_FILE.exists():
                data = read_json(TRANSFER_STATE_FILE)
            else:
                data = {}
        except (OSError, json.JSONDecodeError, TypeError) as e:
            LOGGER.exception("Failed to load transfer state: %s", e)
            data = {}
        if not isinstance(data, dict):
            data = {}

        hub_message_id: Optional[int] = None
        try:
            candidate_hub_id = int(data.get("hub_message_id"))
        except (TypeError, ValueError):
            candidate_hub_id = 0
        if candidate_hub_id > 0:
            hub_message_id = candidate_hub_id

        released_roster_cycles: Dict[str, int] = {}
        raw_released_cycles = data.get("released_roster_cycles", {})
        if isinstance(raw_released_cycles, dict):
            for roster_id, cycle_id in raw_released_cycles.items():
                try:
                    normalized_roster_id = str(int(roster_id))
                    normalized_cycle_id = int(cycle_id)
                except (TypeError, ValueError):
                    continue
                if normalized_cycle_id >= 0:
                    released_roster_cycles[normalized_roster_id] = normalized_cycle_id

        reminder_messages = self._normalize_transfer_reminder_messages(
            data.get("reminder_messages", [])
        )
        canonical = {
            "hub_message_id": hub_message_id,
            "released_roster_cycles": released_roster_cycles,
            "reminder_messages": reminder_messages,
        }
        if data != canonical:
            self._transfer_state_needs_save = True
        return canonical


    def _save_transfer_state(self) -> None:
        # Persist reminder message references for replacement and cleanup.
        try:
            write_json_atomic(TRANSFER_STATE_FILE, self.transfer_state, indent=2)
        except (OSError, TypeError) as e:
            LOGGER.exception("Failed to save transfer state: %s", e)


    def _build_transfer_reminder_message_refs(
        self,
        messages: List[discord.Message],
        delete_at: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        refs: List[Dict[str, Any]] = []
        reminder_delete_at = delete_at or self._build_transfer_reminder_delete_at()
        for message in messages:
            channel_id = getattr(message.channel, "id", None)
            if isinstance(channel_id, int) and channel_id > 0:
                refs.append(
                    {
                        "channel_id": channel_id,
                        "message_id": message.id,
                        "delete_at": reminder_delete_at,
                    }
                )
        return refs


    async def _delete_transfer_reminder_messages(
        self,
        entries: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        unresolved: List[Dict[str, Any]] = []
        channel_cache: Dict[int, Optional[discord.abc.Messageable]] = {}
        failed_channels: Set[int] = set()
        for entry in entries:
            channel_id = entry["channel_id"]
            message_id = entry["message_id"]
            if channel_id in failed_channels:
                unresolved.append(entry)
                continue
            if channel_id not in channel_cache:
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                    except discord.NotFound:
                        channel = None
                    except (discord.Forbidden, discord.HTTPException) as e:
                        LOGGER.warning(
                            "Failed to resolve prior transfer reminder channel %s: %s",
                            channel_id,
                            e,
                        )
                        failed_channels.add(channel_id)
                        unresolved.append(entry)
                        continue
                channel_cache[channel_id] = channel
            channel = channel_cache.get(channel_id)
            if channel is None:
                continue
            try:
                get_partial_message = getattr(channel, "get_partial_message", None)
                if callable(get_partial_message):
                    await get_partial_message(message_id).delete()
                elif hasattr(channel, "fetch_message"):
                    message = await channel.fetch_message(message_id)
                    await message.delete()
                else:
                    LOGGER.warning(
                        "Channel %s cannot delete prior transfer reminder message %s",
                        channel_id,
                        message_id,
                    )
                    unresolved.append(entry)
                    continue
            except discord.NotFound:
                continue
            except (discord.Forbidden, discord.HTTPException) as e:
                LOGGER.warning(
                    "Failed to delete prior transfer reminder message %s in channel %s: %s",
                    message_id,
                    channel_id,
                    e,
                )
                unresolved.append(entry)
        return unresolved


    async def _cleanup_expired_transfer_reminders(self, now: datetime) -> None:
        async with self._transfer_reminder_lock:
            reminders = list(self.transfer_state.get("reminder_messages", []))
            if not reminders:
                return
            active_entries: List[Dict[str, Any]] = []
            expired_entries: List[Dict[str, Any]] = []
            changed = False
            for entry in reminders:
                delete_at = self._parse_transfer_reminder_delete_at(entry.get("delete_at"))
                if delete_at is None:
                    refreshed_entry = dict(entry)
                    refreshed_entry["delete_at"] = self._build_transfer_reminder_delete_at(now)
                    active_entries.append(refreshed_entry)
                    changed = True
                    continue
                if delete_at > now:
                    active_entries.append(entry)
                    continue
                expired_entries.append(entry)

            if expired_entries:
                unresolved_entries = await self._delete_transfer_reminder_messages(expired_entries)
                active_entries.extend(unresolved_entries)
                changed = True

            if changed or len(active_entries) != len(reminders):
                self.transfer_state["reminder_messages"] = active_entries
                self._save_transfer_state()


    async def _resolve_clan_transfers_channel(self) -> Optional[discord.abc.Messageable]:
        channel = self.bot.get_channel(CLAN_TRANSFERS)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(CLAN_TRANSFERS)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        if not hasattr(channel, "send"):
            return None
        return channel


    def _parse_excluded_clans(self, raw: Optional[str]) -> tuple[Set[str], List[str]]:
        # Parse comma/space-separated clan codes for transfer reminders.
        if not raw:
            return set(), []
        tokens = re.split(r"[,\s]+", raw.strip())
        codes = {token.upper() for token in tokens if token}
        valid_codes = set(CWL_CLAN_CODES)
        unknown = sorted(codes - valid_codes)
        return codes & valid_codes, unknown


    @staticmethod
    def _league_group_confirms_spin(
        group: Dict[str, Any],
        expected_season: str,
    ) -> bool:
        if str(group.get("season") or "").strip() != expected_season:
            return False
        state = str(group.get("state") or "").strip()
        if state in {"preparation", "inWar", "warEnded", "ended"}:
            return True
        return any(
            war_tag and war_tag != "#0"
            for round_data in group.get("rounds", []) or []
            for war_tag in round_data.get("warTags", []) or []
        )


    async def _cwl_spin_statuses(
        self,
        clan_codes: Set[str],
        now: Optional[datetime] = None,
    ) -> tuple[Set[str], Set[str]]:
        """Return clans with confirmed spins and clans whose status was unavailable."""
        if not clan_codes:
            return set(), set()
        if not self.clash_client.configured:
            return set(), set(clan_codes)

        expected_season = (now or datetime.now(dt_timezone.utc)).strftime("%Y-%m")

        async def load(clan_code: str) -> tuple[str, str]:
            clan_tag = CWL_CLAN_TAGS.get(clan_code)
            if not clan_tag:
                return clan_code, "unavailable"
            response = await self.clash_client.get(
                f"/clans/{encode_clash_tag(clan_tag)}/currentwar/leaguegroup",
                attempts=1,
                timeout_seconds=15,
            )
            if response.status == 404:
                return clan_code, "not_started"
            group = response.payload_object
            if response.status != 200 or group is None:
                LOGGER.warning(
                    "Could not check CWL spin status clan=%s status=%s error=%s",
                    clan_code,
                    response.status,
                    response.error,
                )
                return clan_code, "unavailable"
            if self._league_group_confirms_spin(group, expected_season):
                return clan_code, "started"
            return clan_code, "not_started"

        statuses = await asyncio.gather(*(load(code) for code in clan_codes))
        started = {code for code, status in statuses if status == "started"}
        unavailable = {code for code, status in statuses if status == "unavailable"}
        return started, unavailable

    @staticmethod
    def _native_roster_mismatches(
        roster_members: Dict[str, List[RosterMember]],
        profiles: Dict[str, LinkedAccount],
    ) -> Dict[str, List[int]]:
        mismatches: Dict[str, List[int]] = {}
        for clan_code, members in roster_members.items():
            mismatched_ids: List[int] = []
            seen_ids: Set[int] = set()
            for member in members:
                profile = profiles.get(member.player_tag)
                if profile is None or profile.clan_code == clan_code:
                    continue
                if member.discord_user_id not in seen_ids:
                    seen_ids.add(member.discord_user_id)
                    mismatched_ids.append(member.discord_user_id)
            if mismatched_ids:
                mismatches[clan_code] = mismatched_ids
        return mismatches


    def _chunk_content(self, content: str, max_len: int = 1900) -> List[str]:
        # Split content by lines first, then hard-wrap any oversized line.
        if not content:
            return [""]
        chunks: List[str] = []
        current = ""
        for line in content.splitlines():
            if len(line) > max_len:
                if current:
                    chunks.append(current)
                    current = ""
                start = 0
                while start < len(line):
                    chunks.append(line[start:start + max_len])
                    start += max_len
                continue
            candidate = line if not current else f"{current}\n{line}"
            if len(candidate) <= max_len:
                current = candidate
            else:
                chunks.append(current)
                current = line
        if current:
            chunks.append(current)
        return chunks or [""]


    def _extract_reaction_emojis(self, text: str) -> List[str]:
        # Detect custom + unicode emojis in send order for controlled auto-reactions.
        custom_emoji_re = re.compile(r"<a?:\w+:\d+>")
        unicode_emoji_re = re.compile(
            r"[\U0001F1E6-\U0001F1FF]|"
            r"[\U0001F300-\U0001F6FF]|"
            r"[\U0001F700-\U0001F77F]|"
            r"[\U00002600-\U000026FF]"
        )
        found: List[str] = []
        seen: Set[str] = set()
        for match in custom_emoji_re.finditer(text or ""):
            emoji_token = match.group(0)
            if emoji_token not in seen:
                seen.add(emoji_token)
                found.append(emoji_token)
        for match in unicode_emoji_re.finditer(text or ""):
            emoji_token = match.group(0)
            if emoji_token not in seen:
                seen.add(emoji_token)
                found.append(emoji_token)
        return found


    async def _react_with_detected_emojis(self, message: discord.Message, text: str) -> None:
        # Add detected emojis to the target message; ignore reaction failures.
        for emoji_token in self._extract_reaction_emojis(text):
            try:
                await message.add_reaction(emoji_token)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                continue


    async def _send_chunked(self, channel: discord.abc.Messageable, content: str, **kwargs) -> List[discord.Message]:
        # Split long reminders across multiple messages to fit Discord limits.
        sent_messages: List[discord.Message] = []
        for chunk in self._chunk_content(content):
            sent_messages.append(await channel.send(chunk, **kwargs))
        return sent_messages


    async def _run_transfer_reminder(
        self,
        interaction: discord.Interaction,
        excluded_clans: Optional[Set[str]] = None,
    ) -> None:
        excluded_clans = excluded_clans or set()
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if not self.clash_client.configured:
            await interaction.followup.send(
                "CWL transfer checks aren't available because Clash API access hasn't been set up.",
                ephemeral=True,
            )
            return
        candidate_codes = set(CWL_CLAN_ROSTER_IDS) & set(CWL_CLAN_CODES) - excluded_clans
        if not candidate_codes:
            await interaction.followup.send("Every CWL roster was left out of this check.", ephemeral=True)
            return

        status_message = await interaction.followup.send(
            "Checking CWL rosters...",
            ephemeral=True,
            wait=True,
        )

        async def finish_status(lines: List[str]) -> None:
            final_content = "\n".join(lines)
            try:
                await status_message.edit(content=final_content)
            except discord.HTTPException:
                LOGGER.debug("Unable to replace transfer reminder progress message")
                await interaction.followup.send(final_content, ephemeral=True)

        started_clans, unavailable_statuses = await self._cwl_spin_statuses(candidate_codes)
        checked_codes = candidate_codes - started_clans - unavailable_statuses
        roster_members: Dict[str, List[RosterMember]] = {}
        unavailable_rosters: Set[str] = set()
        empty_rosters: Set[str] = set()
        for clan_code in CWL_CLAN_CODES:
            if clan_code not in checked_codes:
                continue
            roster_id = CWL_CLAN_ROSTER_IDS[clan_code]
            try:
                roster = await self.roster_queries.get(roster_id)
                if (
                    roster is None
                    or roster.clan_code != clan_code
                    or roster.guild_id != interaction.guild_id
                ):
                    unavailable_rosters.add(clan_code)
                    continue
                members = await self.roster_queries.members(roster)
            except (OSError, sqlite3.Error):
                LOGGER.exception("Could not read native CWL roster clan=%s", clan_code)
                unavailable_rosters.add(clan_code)
                continue
            roster_members[clan_code] = members
            if not members:
                empty_rosters.add(clan_code)

        accounts_by_tag: Dict[str, LinkedAccount] = {}
        for members in roster_members.values():
            for member in members:
                accounts_by_tag.setdefault(
                    member.player_tag,
                    LinkedAccount(
                        player_tag=member.player_tag,
                        player_name=member.player_name,
                        clan_code=member.clan_code,
                        townhall=member.townhall,
                        hero_sum=member.hero_sum,
                    ),
                )
        profiles, failed_tags = await fetch_account_profiles(
            list(accounts_by_tag.values()),
            self.clash_client,
        )
        incomplete_account_rosters = {
            clan_code
            for clan_code, members in roster_members.items()
            if any(member.player_tag in failed_tags for member in members)
        }

        result_lines: List[str] = []
        if started_clans:
            result_lines.append(
                f"CWL has started for: {', '.join(code for code in CWL_CLAN_CODES if code in started_clans)}. "
                "Those rosters were left out."
            )
        if unavailable_statuses:
            unavailable_status_labels = ", ".join(
                code for code in CWL_CLAN_CODES if code in unavailable_statuses
            )
            result_lines.append(
                f"I couldn't check whether CWL has started for: {unavailable_status_labels}."
            )
        if unavailable_rosters:
            unavailable_roster_labels = ", ".join(
                code for code in CWL_CLAN_CODES if code in unavailable_rosters
            )
            result_lines.append(
                f"I couldn't check these rosters: {unavailable_roster_labels}."
            )
        if incomplete_account_rosters:
            incomplete_account_labels = ", ".join(
                code for code in CWL_CLAN_CODES if code in incomplete_account_rosters
            )
            result_lines.append(
                f"I couldn't check every account on: {incomplete_account_labels}."
            )
        if empty_rosters and any(roster_members.values()):
            result_lines.append(
                f"No players are on: {', '.join(code for code in CWL_CLAN_CODES if code in empty_rosters)}."
            )

        if unavailable_statuses or unavailable_rosters or incomplete_account_rosters:
            result_lines.insert(0, "I couldn't check every CWL roster, so the reminder wasn't changed.")
            await finish_status(result_lines)
            return

        mismatches = self._native_roster_mismatches(roster_members, profiles)

        content: Optional[str] = None
        target_channel: Optional[discord.abc.Messageable] = None
        if not mismatches:
            if roster_members and any(roster_members.values()):
                result_lines.insert(0, "No one in the checked rosters still needs to transfer.")
            elif started_clans:
                result_lines.insert(0, "No transfer reminder is needed.")
            else:
                result_lines.insert(0, "No players are on the checked rosters.")
        else:
            lines: List[str] = [
                "# CWL Transfer Reminder",
                "",
                "## Please move to your CWL clan as soon as possible so you don't miss "
                "the spin <:pray:1209861423963045928>",
                "",
            ]
            ordered_clans = CWL_CLAN_CODES
            for code in ordered_clans:
                ids = mismatches.get(code)
                if not ids:
                    continue
                clan_name = CLAN_NAMES.get(code, code)
                # Build mentions from user IDs so tags are always accurate.
                mentions = " ".join(f"<@{user_id}>" for user_id in ids)
                lines.append(f"### {mentions}")
                lines.append(f"Move to {clan_name} ({code}) for CWL.")
                lines.append("")
            lines.append("### Clans have been linked below for easier access <:hold:1353791394078265406>")
            lines.append("\n".join(
                f"{code}: {CLAN_LINKS[code]}"
                for code in ordered_clans
                if code in mismatches and code in CLAN_LINKS
            ))

            content = "\n".join(lines)
            target_channel = await self._resolve_clan_transfers_channel()
            if target_channel is None:
                result_lines.append(
                    "The CWL transfer channel hasn't been set up, so no reminder was posted. Check the CWL setup."
                )
        if not mismatches:
            async with self._transfer_reminder_lock:
                previous_entries = list(self.transfer_state.get("reminder_messages", []))
                unresolved_entries = await self._delete_transfer_reminder_messages(previous_entries)
                self.transfer_state["reminder_messages"] = unresolved_entries
                self._save_transfer_state()
            if unresolved_entries:
                result_lines.append("I couldn't clear the previous reminder.")
            await finish_status(result_lines)
            return
        if target_channel is None:
            await finish_status(result_lines)
            return
        async with self._transfer_reminder_lock:
            previous_entries = list(self.transfer_state.get("reminder_messages", []))
            unresolved_entries = await self._delete_transfer_reminder_messages(previous_entries)
            if unresolved_entries:
                self.transfer_state["reminder_messages"] = unresolved_entries
                self._save_transfer_state()
                result_lines.insert(0, "Couldn't clear the old reminder, so no new one was posted.")
                await finish_status(result_lines)
                return
            if previous_entries:
                self.transfer_state["reminder_messages"] = []
                self._save_transfer_state()

            sent_messages: List[discord.Message] = []
            try:
                for chunk in self._chunk_content(content):
                    sent_messages.append(
                        await target_channel.send(
                            chunk,
                            allowed_mentions=discord.AllowedMentions(
                                users=True,
                                roles=False,
                                everyone=False,
                            ),
                        )
                    )
            except (discord.Forbidden, discord.HTTPException) as e:
                LOGGER.warning("Failed to post transfer reminder: %s", e)
                partial_reminder_visible = False
                if sent_messages:
                    partial_entries = self._build_transfer_reminder_message_refs(sent_messages)
                    unresolved_partial_entries = await self._delete_transfer_reminder_messages(
                        partial_entries
                    )
                    if unresolved_partial_entries:
                        partial_reminder_visible = True
                        self.transfer_state["reminder_messages"] = unresolved_partial_entries
                        self._save_transfer_state()
                if partial_reminder_visible:
                    failure_message = (
                        "I couldn't post the full transfer reminder. Part of it may still be "
                        "visible in the CWL transfer channel."
                    )
                else:
                    failure_message = "I couldn't post the transfer reminder. Nothing was posted."
                result_lines.insert(0, failure_message)
                await finish_status(result_lines)
                return

            self.transfer_state["reminder_messages"] = self._build_transfer_reminder_message_refs(
                sent_messages
            )
            self._save_transfer_state()
        result_lines.insert(0, "Transfer reminder posted.")
        await finish_status(result_lines)


    @app_commands.describe(
        exclude="Clan codes to leave out of this reminder.",
    )
    async def transfer_reminder(
        self,
        interaction: discord.Interaction,
        exclude: Optional[str] = None,
    ):
        if not self._has_any_role(interaction, (LEAD_PLUS | CWL_HELPERS)):
            await deny(interaction)
            return
        excluded_clans, unknown = self._parse_excluded_clans(exclude)
        if unknown:
            await interaction.response.send_message(
                f"Unrecognized clans: {', '.join(unknown)}. Choose from: {', '.join(sorted(CWL_CLAN_CODES))}.",
                ephemeral=True,
            )
            return
        await self._run_transfer_reminder(interaction, excluded_clans)

"""Live rendering, controls, and sticky lifecycle for CWL thread boards."""

from __future__ import annotations

import logging
import time
from typing import Any

import discord

from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.discord.interactions import deny
from elbow_helper.discord.interactions import warn

from ..config import CLAN_LINKS
from ..config import CLAN_NAME_TO_CODE
from ..config import CWL_CLAN_NAMES
from ..config import CWL_CLAN_TAGS
from ..config import THREAD_SNAPSHOT_MAX_AGE_SECONDS
from ..config import THREAD_STICKY_BURIAL_MESSAGES
from ..config import THREAD_STICKY_BURIAL_SCAN_LIMIT
from ..config import THREAD_STICKY_REPOST_COOLDOWN_SECONDS
from .snapshots import CwlThreadRound
from .snapshots import CwlThreadSnapshot
from .snapshots import build_cwl_thread_snapshot
from .snapshots import cwl_thread_snapshot_is_complete
from .views import CwlCcStatusView


LOGGER = logging.getLogger(__name__)
VALID_CC_STATUSES = frozenset({"filled", "partial", "empty"})
CC_STATUS_TEXT = {
    "filled": "CCs filled",
    "partial": "CCs partially filled",
    "empty": "CCs empty",
}


def _embed_without_timestamp(embed: discord.Embed) -> dict[str, Any]:
    payload = embed.to_dict()
    payload.pop("timestamp", None)
    return payload


def _message_component_payload(message: discord.Message) -> list[dict[str, Any]]:
    return [component.to_dict() for component in message.components]


def _view_component_payload(view: discord.ui.View | None) -> list[dict[str, Any]]:
    return view.to_components() if view is not None else []


def _escaped(value: str) -> str:
    return discord.utils.escape_markdown(value)


class CwlThreadBoardMixin:
    def _registered_thread_entry(
        self,
        clan_code: str,
    ) -> tuple[str, str, dict[str, Any]] | None:
        clan_name = CWL_CLAN_NAMES.get(clan_code)
        if clan_name is None:
            return None
        thread_id = self.clan_configs.get(clan_name, {}).get("thread_id")
        if thread_id is None:
            return None
        thread_key = str(thread_id)
        thread_data = self.data.get("threads", {}).get(thread_key)
        if not isinstance(thread_data, dict):
            return None
        return clan_name, thread_key, thread_data

    def _remember_thread_snapshot(
        self,
        clan_code: str,
        wars: list[dict[str, Any]],
        snapshot: CwlThreadSnapshot,
    ) -> None:
        self._thread_snapshot_cache[clan_code] = (
            time.monotonic(),
            wars,
            snapshot,
        )

    def _validated_thread_snapshot(
        self,
        clan_code: str,
        wars: list[dict[str, Any]],
    ) -> CwlThreadSnapshot | None:
        clan_tag = CWL_CLAN_TAGS.get(clan_code)
        if clan_tag is None:
            return None
        now = self._utc_now()
        if not cwl_thread_snapshot_is_complete(wars, now=now):
            return None
        snapshot = build_cwl_thread_snapshot(wars, clan_tag, now=now)
        active_rounds = tuple(
            active
            for active in (snapshot.battle, snapshot.preparation)
            if active is not None
        )
        raw_active_count = sum(
            1
            for war in wars
            if str(war.get("_state") or war.get("state") or "")
            .replace("_", "")
            .casefold()
            in {"inwar", "preparation"}
        )
        if len(active_rounds) < raw_active_count:
            return None
        if any(
            active.is_stale
            or active.round_number <= 0
            or not active.war_tag
            or not active.season
            or (active.state == "inwar" and active.attacks_total <= 0)
            for active in active_rounds
        ):
            return None
        self._remember_thread_snapshot(clan_code, wars, snapshot)
        return snapshot

    async def _latest_thread_snapshot(
        self,
        clan_code: str,
        *,
        force_refresh: bool = False,
    ) -> tuple[list[dict[str, Any]], CwlThreadSnapshot] | None:
        cached = self._thread_snapshot_cache.get(clan_code)
        if cached is not None and not force_refresh:
            cached_at, wars, snapshot = cached
            if time.monotonic() - cached_at <= THREAD_SNAPSHOT_MAX_AGE_SECONDS:
                return wars, snapshot

        wars = await self._get_league_wars(clan_code)
        snapshot = self._validated_thread_snapshot(clan_code, wars)
        if snapshot is None:
            return None
        return wars, snapshot

    @staticmethod
    def _active_prep_state(thread_data: dict[str, Any]) -> dict[str, Any] | None:
        active = thread_data.get("active_prep")
        return active if isinstance(active, dict) else None

    @staticmethod
    def _cc_status_records(thread_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        records = thread_data.setdefault("cc_statuses", {})
        if not isinstance(records, dict):
            records = {}
            thread_data["cc_statuses"] = records
        return records

    def _prepare_cc_state(
        self,
        thread_data: dict[str, Any],
        preparation: CwlThreadRound | None,
    ) -> tuple[str | None, bool]:
        changed = False
        records = self._cc_status_records(thread_data)
        active_war_tag = preparation.war_tag if preparation is not None else None
        for war_tag in tuple(records):
            record = records.get(war_tag)
            if war_tag != active_war_tag or not isinstance(record, dict):
                records.pop(war_tag, None)
                changed = True
                continue
            status = str(record.get("status") or "").casefold()
            if status not in VALID_CC_STATUSES:
                records.pop(war_tag, None)
                changed = True

        if preparation is None or not preparation.war_tag:
            if thread_data.pop("active_prep", None) is not None:
                changed = True
            return None, changed

        record = records.get(preparation.war_tag)
        status = (
            str(record.get("status") or "").casefold()
            if isinstance(record, dict)
            else ""
        )
        if status not in VALID_CC_STATUSES:
            legacy = thread_data.get("cc_status")
            legacy_status = (
                str(legacy.get(str(preparation.round_number)) or "").casefold()
                if isinstance(legacy, dict)
                else ""
            )
            status = legacy_status if legacy_status in VALID_CC_STATUSES else "empty"
            records[preparation.war_tag] = {
                "round": preparation.round_number,
                "season": preparation.season,
                "status": status,
            }
            changed = True

        active_prep = {
            "war_tag": preparation.war_tag,
            "round": preparation.round_number,
            "season": preparation.season,
        }
        if thread_data.get("active_prep") != active_prep:
            thread_data["active_prep"] = active_prep
            changed = True
        if isinstance(thread_data.get("cc_status"), dict) and thread_data["cc_status"]:
            thread_data["cc_status"] = {}
            changed = True
        return status, changed

    async def _build_thread_status_board(
        self,
        clan_code: str,
        snapshot: CwlThreadSnapshot,
        status: str | None,
    ) -> tuple[discord.Embed, CwlCcStatusView | None]:
        emojis = await self.cwl_thread_emojis.get()
        anchor = snapshot.battle or snapshot.preparation
        if anchor is None:
            raise ValueError("An active CWL round is required to build the thread board")

        badge_url = anchor.clan_badge_url
        if not badge_url:
            candidate = self.account_links.get_clan_badge_url(clan_code)
            if isinstance(candidate, str) and candidate:
                badge_url = candidate

        embed = discord.Embed(
            title="CWL Status",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=self._utc_now(),
        )
        embed.set_author(
            name=f"{anchor.clan_name}",
            url=CLAN_LINKS.get(clan_code),
            icon_url=badge_url,
        )

        sections = []
        if snapshot.battle is not None:
            battle = snapshot.battle
            battle_lines = [
                f"{battle.attacks_used}/{battle.attacks_total} attacks"
            ]
            if battle.end_at is not None:
                battle_lines.append(
                    f"Ends {discord.utils.format_dt(battle.end_at, 'R')}"
                )
            if battle.missing_attacks:
                empty_sword = emojis.icon("empty_sword", "⚠️")
                missing = ", ".join(_escaped(name) for name in battle.missing_attacks)
                battle_lines.append(f"{empty_sword} Missing: {missing}")
            war_icon = emojis.icon("war", "⚔️")
            sections.append(
                "\n".join(
                    (
                        f"**{war_icon} Day {battle.round_number} · Battle vs "
                        f"{_escaped(battle.opponent_name)}**",
                        *battle_lines,
                    )
                )
            )

        view = None
        if snapshot.preparation is not None and status is not None:
            preparation = snapshot.preparation
            preparation_lines = [CC_STATUS_TEXT[status]]
            if preparation.start_at is not None:
                preparation_lines.append(
                    "Battle starts "
                    f"{discord.utils.format_dt(preparation.start_at, 'R')}"
                )
            clan_castle = emojis.icon("clan_castle", "🏰")
            sections.append(
                "\n".join(
                    (
                        f"**{clan_castle} Day {preparation.round_number} · Preparation vs "
                        f"{_escaped(preparation.opponent_name)}**",
                        *preparation_lines,
                    )
                )
            )
            view = CwlCcStatusView(
                self,
                clan_code,
                current_status=status,
                emojis=emojis,
            )

        embed.description = "\n\n".join(sections)
        embed.set_footer(text="Last updated")
        return embed, view

    async def sync_registered_cwl_thread(
        self,
        clan_code: str,
        wars: list[dict[str, Any]],
    ) -> bool:
        try:
            return await self._sync_registered_cwl_thread(clan_code, wars)
        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
            LOGGER.exception("Failed to sync the registered CWL thread for %s", clan_code)
            return False

    async def _sync_registered_cwl_thread(
        self,
        clan_code: str,
        wars: list[dict[str, Any]],
    ) -> bool:
        snapshot = self._validated_thread_snapshot(clan_code, wars)
        if snapshot is None:
            return False
        registration = self._registered_thread_entry(clan_code)
        if registration is None:
            return True
        _, thread_key, thread_data = registration
        if not snapshot.has_active_round:
            thread_data["cc_status"] = {}
            thread_data["cc_statuses"] = {}
            thread_data.pop("active_prep", None)
            self.save_data()
            return await self._remove_thread_status_board(thread_key, thread_data)

        thread = await self._resolve_registered_thread(thread_key)
        if thread is None:
            return False
        if getattr(thread, "archived", False) or getattr(thread, "locked", False):
            if not await self._set_registered_thread_archived(
                thread,
                archived=False,
                reason="Show the active CWL status board",
            ):
                return False
        return await self._sync_thread_status_board(
            clan_code,
            thread,
            thread_data,
            snapshot,
        )

    async def _sync_thread_status_board(
        self,
        clan_code: str,
        thread: discord.Thread,
        thread_data: dict[str, Any],
        snapshot: CwlThreadSnapshot,
    ) -> bool:
        lock = self._get_sticky_lock(str(thread.id))
        async with lock:
            status, state_changed = self._prepare_cc_state(
                thread_data,
                snapshot.preparation,
            )
            if state_changed:
                self.save_data()
            embed, view = await self._build_thread_status_board(
                clan_code,
                snapshot,
                status,
            )
            old_message = None
            old_message_id = thread_data.get("sticky_message_id")
            if old_message_id:
                try:
                    completed, old_message = await self._run_sticky_http_operation(
                        thread,
                        "fetch CWL status board",
                        lambda: thread.fetch_message(int(old_message_id)),
                    )
                    if not completed:
                        return False
                except (discord.NotFound, TypeError, ValueError):
                    old_message = None

            if old_message is not None:
                embeds_match = (
                    len(old_message.embeds) == 1
                    and _embed_without_timestamp(old_message.embeds[0])
                    == _embed_without_timestamp(embed)
                )
                components_match = (
                    _message_component_payload(old_message)
                    == _view_component_payload(view)
                )
                if embeds_match and components_match:
                    if await self._cleanup_stale_sticky_messages(
                        thread,
                        thread_data,
                        current_sticky_id=old_message.id,
                    ):
                        self.save_data()
                    return True
                try:
                    completed, _ = await self._run_sticky_http_operation(
                        thread,
                        "edit CWL status board",
                        lambda: old_message.edit(embed=embed, view=view),
                    )
                except discord.NotFound:
                    old_message = None
                else:
                    if not completed:
                        return False
                    thread_data["sticky_last_updated"] = self._utc_now_iso()
                    await self._cleanup_stale_sticky_messages(
                        thread,
                        thread_data,
                        current_sticky_id=old_message.id,
                    )
                    self.save_data()
                    return True

            completed, new_message = await self._run_sticky_http_operation(
                thread,
                "send CWL status board",
                lambda: thread.send(embed=embed, view=view),
            )
            if not completed or new_message is None:
                return False
            thread_data["sticky_message_id"] = new_message.id
            thread_data["sticky_last_updated"] = self._utc_now_iso()
            self.save_data()
            return True

    async def _remove_thread_status_board(
        self,
        thread_key: str,
        thread_data: dict[str, Any],
    ) -> bool:
        message_id = thread_data.get("sticky_message_id")
        if not message_id:
            return True
        thread = await self._resolve_registered_thread(thread_key)
        if thread is None:
            return False
        lock = self._get_sticky_lock(thread_key)
        async with lock:
            try:
                completed, message = await self._run_sticky_http_operation(
                    thread,
                    "fetch finished CWL status board",
                    lambda: thread.fetch_message(int(message_id)),
                )
                if not completed:
                    return False
            except (discord.NotFound, TypeError, ValueError):
                message = None
            if message is not None:
                try:
                    completed, _ = await self._run_sticky_http_operation(
                        thread,
                        "delete finished CWL status board",
                        lambda: message.delete(),
                    )
                    if not completed:
                        return False
                except discord.NotFound:
                    pass
            thread_data["sticky_message_id"] = None
            thread_data.pop("sticky_last_updated", None)
            if await self._cleanup_stale_sticky_messages(
                thread,
                thread_data,
                current_sticky_id=None,
            ):
                self.save_data()
            self.save_data()
            return True

    async def _repost_thread_status_from_activity(
        self,
        thread: discord.Thread,
    ) -> bool:
        thread_key = str(thread.id)
        thread_data = self.data.get("threads", {}).get(thread_key)
        if not isinstance(thread_data, dict):
            return False
        board_message_id = thread_data.get("sticky_message_id")
        if not board_message_id:
            return False
        last_repost_at = self._sticky_last_repost_at.get(thread_key)
        if (
            last_repost_at is not None
            and time.monotonic() - last_repost_at
            < THREAD_STICKY_REPOST_COOLDOWN_SECONDS
        ):
            return False

        newer_human_messages = 0
        try:
            async for message in thread.history(
                limit=THREAD_STICKY_BURIAL_SCAN_LIMIT,
                after=discord.Object(id=int(board_message_id)),
                oldest_first=False,
            ):
                if message.id == board_message_id or getattr(message.author, "bot", False):
                    continue
                newer_human_messages += 1
                if newer_human_messages >= THREAD_STICKY_BURIAL_MESSAGES:
                    break
        except (discord.Forbidden, discord.HTTPException, TypeError, ValueError):
            return False
        if newer_human_messages < THREAD_STICKY_BURIAL_MESSAGES:
            return False
        return await self._repost_existing_thread_status(
            thread,
            thread_data,
            expected_message_id=int(board_message_id),
        )

    async def _repost_existing_thread_status(
        self,
        thread: discord.Thread,
        thread_data: dict[str, Any],
        *,
        expected_message_id: int,
    ) -> bool:
        thread_key = str(thread.id)
        lock = self._get_sticky_lock(thread_key)
        async with lock:
            if thread_data.get("sticky_message_id") != expected_message_id:
                return False
            try:
                completed, old_message = await self._run_sticky_http_operation(
                    thread,
                    "fetch buried CWL status board",
                    lambda: thread.fetch_message(expected_message_id),
                )
                if not completed or old_message is None:
                    return False
            except discord.NotFound:
                return False
            if not old_message.embeds:
                return False

            active_prep = self._active_prep_state(thread_data)
            view = None
            if active_prep is not None:
                clan_name = str(thread_data.get("clan_name") or "")
                clan_code = CLAN_NAME_TO_CODE.get(clan_name)
                war_tag = str(active_prep.get("war_tag") or "")
                record = self._cc_status_records(thread_data).get(war_tag, {})
                status = str(record.get("status") or "empty").casefold()
                if clan_code is not None and status in VALID_CC_STATUSES:
                    emojis = await self.cwl_thread_emojis.get()
                    view = CwlCcStatusView(
                        self,
                        clan_code,
                        current_status=status,
                        emojis=emojis,
                    )

            completed, new_message = await self._run_sticky_http_operation(
                thread,
                "repost buried CWL status board",
                lambda: thread.send(embed=old_message.embeds[0], view=view),
            )
            if not completed or new_message is None:
                return False
            stale_ids = self._get_pending_stale_sticky_ids(
                thread_data,
                current_sticky_id=new_message.id,
            )
            if expected_message_id not in stale_ids:
                stale_ids.append(expected_message_id)
            thread_data["sticky_message_id"] = new_message.id
            self._set_pending_stale_sticky_ids(thread_data, stale_ids)
            self._sticky_last_repost_at[thread_key] = time.monotonic()
            self.save_data()
            if await self._cleanup_stale_sticky_messages(
                thread,
                thread_data,
                current_sticky_id=new_message.id,
            ):
                self.save_data()
            return True

    async def refresh_registered_cwl_status_for_clan(self, clan_code: str) -> bool:
        latest = await self._latest_thread_snapshot(clan_code, force_refresh=True)
        if latest is None:
            return False
        wars, _ = latest
        return await self.sync_registered_cwl_thread(clan_code, wars)

    async def _record_active_cc_status(
        self,
        interaction: discord.Interaction,
        clan_code: str,
        status: str,
    ) -> None:
        if status not in VALID_CC_STATUSES:
            await warn(interaction, "That Clan Castle status isn't valid.")
            return
        if not self.check_permissions(interaction, require_leader=False):
            await deny(interaction)
            return
        if not isinstance(interaction.channel, discord.Thread):
            await warn(interaction, "This CWL status post is no longer active.")
            return

        registration = self._registered_thread_entry(clan_code)
        if registration is None:
            await warn(interaction, "This thread isn't set up for CWL.")
            return
        _, thread_key, thread_data = registration
        if str(interaction.channel.id) != thread_key:
            await warn(interaction, "This CWL status post is no longer active.")
            return
        if interaction.message is not None:
            if interaction.message.id != thread_data.get("sticky_message_id"):
                await warn(interaction, "This is an older CWL status post. Use the latest one.")
                return

        await interaction.response.defer(ephemeral=True)
        latest = await self._latest_thread_snapshot(clan_code, force_refresh=True)
        if latest is None:
            await interaction.followup.send(
                "CWL data is unavailable right now. Nothing was changed.",
                ephemeral=True,
            )
            return
        wars, snapshot = latest
        preparation = snapshot.preparation
        if preparation is None or not preparation.war_tag:
            await self.sync_registered_cwl_thread(clan_code, wars)
            await interaction.followup.send(
                "No CWL preparation day is active right now.",
                ephemeral=True,
            )
            return
        displayed = self._active_prep_state(thread_data)
        displayed_tag = str(displayed.get("war_tag") or "") if displayed else ""
        if displayed_tag and displayed_tag != preparation.war_tag:
            await self.sync_registered_cwl_thread(clan_code, wars)
            await interaction.followup.send(
                "The preparation round changed. Use the updated status post.",
                ephemeral=True,
            )
            return

        records = self._cc_status_records(thread_data)
        records[preparation.war_tag] = {
            "round": preparation.round_number,
            "season": preparation.season,
            "status": status,
            "updated_at": self._utc_now_iso(),
            "updated_by": interaction.user.id,
        }
        thread_data["active_prep"] = {
            "war_tag": preparation.war_tag,
            "round": preparation.round_number,
            "season": preparation.season,
        }
        self.save_data()
        refreshed = await self.sync_registered_cwl_thread(clan_code, wars)
        message = f"Day {preparation.round_number} CCs marked **{status}**."
        if not refreshed:
            message += " The status post couldn't be refreshed."
        await interaction.followup.send(message, ephemeral=True)

    async def update_cc_status_from_button(
        self,
        interaction: discord.Interaction,
        clan_code: str,
        status: str,
    ) -> None:
        await self._record_active_cc_status(interaction, clan_code, status)

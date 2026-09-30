"""Slash command handlers for clan transfers."""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone as dt_timezone

import discord
from discord import app_commands

from .config import CLAN_CHOICES
from .config import CLAN_TRANSFER_QUEUES
from .config import CWL_SEASON_TRANSFER_CLANS


class ClanTransfersCommandMixin:
    def transfer_request_status(self, clan_code: str, member_id: int) -> bool:
        queue = self.queries.pending_snapshot(clan_codes=(clan_code,)).queues[0]
        return any(row.member_id == member_id for row in queue.pending)

    def transfer_request_preview(self, clan_code: str, member_id: int,
                                 *, cancel: bool = False) -> dict[str, object]:
        if clan_code not in CLAN_TRANSFER_QUEUES:
            return {"issue": "That transfer destination is unavailable."}
        if (not cancel and clan_code in CWL_SEASON_TRANSFER_CLANS
                and not self._is_cwl_season_transfer_window()):
            return {"issue": f"Transfers to {clan_code} open around CWL season."}
        queue = self.queries.pending_snapshot(clan_codes=(clan_code,)).queues[0]
        pending = any(row.member_id == member_id for row in queue.pending)
        if cancel and not pending:
            return {"issue": f"You don't have a pending transfer request for {clan_code}."}
        if not cancel and pending:
            return {"issue": f"You already have a request for {clan_code}. Use /transfer cancel to remove it."}
        return {
            "issue": None, "clan_code": clan_code, "member_id": member_id,
            "cancel": cancel, "pending_count": len(queue.pending),
            "thread_id": queue.thread_id,
            "notify_role_ids": tuple(self._get_notify_role_ids(clan_code)) if not cancel and not queue.pending else (),
        }

    async def request_transfer(self, clan_code: str, member_id: int) -> str:
        if clan_code in CWL_SEASON_TRANSFER_CLANS and not self._is_cwl_season_transfer_window():
            return f"Transfers to {clan_code} open around CWL season."
        async with self.locks[clan_code]:
            await self._expire_stale_requests(clan_code, lock_held=True)
            clan_state = self._get_clan_state(clan_code)
            pending = clan_state.get("pending", [])
            if any(entry["user_id"] == member_id for entry in pending):
                return f"You already have a request for {clan_code}. Use /transfer cancel to remove it."
            pending.append({"user_id": member_id, "created_at": self._now().isoformat()})
            was_empty = len(pending) == 1
            clan_state["pending"] = pending
            self._save_state()
        await self.ensure_queue_message(clan_code)
        await self.ensure_global_board()
        if was_empty:
            await self._send_ping(clan_code)
        return f"Added you to the {clan_code} transfer queue."

    async def cancel_transfer(self, clan_code: str, member_id: int) -> str:
        async with self.locks[clan_code]:
            await self._expire_stale_requests(clan_code, lock_held=True)
            clan_state = self._get_clan_state(clan_code)
            pending = clan_state.get("pending", [])
            new_pending = [entry for entry in pending if entry["user_id"] != member_id]
            if len(new_pending) == len(pending):
                return f"You don't have a pending transfer request for {clan_code}."
            clan_state["pending"] = new_pending
            if not new_pending:
                await self._delete_ping(clan_code)
            self._save_state()
        await self.ensure_queue_message(clan_code)
        await self.ensure_global_board()
        return f"Cancelled your request for {clan_code}."

    @staticmethod
    def _next_month_start(month_start: date) -> date:
        if month_start.month == 12:
            return date(month_start.year + 1, 1, 1)
        return date(month_start.year, month_start.month + 1, 1)

    def _is_cwl_season_transfer_window(self) -> bool:
        now = datetime.now(dt_timezone.utc)
        today = now.date()
        month_start = date(now.year, now.month, 1)
        for cwl_month_start in (month_start, self._next_month_start(month_start)):
            window_start = cwl_month_start - timedelta(days=2)
            window_end = cwl_month_start + timedelta(days=10)
            if window_start <= today <= window_end:
                return True
        return False

    @app_commands.describe(destination="Family clan you want to move to.")
    @app_commands.choices(destination=CLAN_CHOICES)
    async def transfer_request(self, interaction: discord.Interaction, destination: app_commands.Choice[str]) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except (discord.InteractionResponded, discord.NotFound):
            self.logger.debug(
                "Transfer request defer skipped: user_id=%s clan=%s",
                interaction.user.id,
                destination.value,
            )

        reply = await self.request_transfer(destination.value, interaction.user.id)
        await self._safe_ephemeral_reply(interaction, reply)

    @app_commands.describe(destination="Destination of the transfer request you want to cancel.")
    @app_commands.choices(destination=CLAN_CHOICES)
    async def transfer_cancel(self, interaction: discord.Interaction, destination: app_commands.Choice[str]) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except (discord.InteractionResponded, discord.NotFound):
            self.logger.debug(
                "Transfer cancel defer skipped: user_id=%s clan=%s",
                interaction.user.id,
                destination.value,
            )

        reply = await self.cancel_transfer(destination.value, interaction.user.id)
        await self._safe_ephemeral_reply(interaction, reply)

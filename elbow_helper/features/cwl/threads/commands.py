"""Thread feature command handlers and permission helpers."""

from __future__ import annotations

import logging
import copy

import discord
from discord import app_commands
from elbow_helper.discord.interactions import deny
from elbow_helper.discord.interactions import fail
from elbow_helper.discord.interactions import warn

from elbow_helper.configuration.roles import CORE
from ..config import CLAN_NAME_TO_CODE
from ..config import THREAD_CLAN_CHOICES


LOGGER = logging.getLogger(__name__)


class CwlThreadCommandMixin:
    async def prepare_cwl_thread_registration(self, clan: str,
                                              thread_id: str) -> dict[str, object]:
        try:
            thread_id_int = int(thread_id)
        except ValueError:
            return {"issue": "That thread ID isn't valid. It should be a number — you can get it from the thread URL.",
                    "invalid_id": True}
        thread = self.bot.get_channel(thread_id_int)
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(thread_id_int)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                thread = None
        if not thread or not isinstance(thread, discord.Thread):
            return {"issue": "I couldn't find that thread. Check the thread ID and try again."}
        threads = self.data.get("threads", {})
        thread_key = str(thread.id)
        existing_thread_data = threads.get(thread_key)
        if isinstance(existing_thread_data, dict):
            existing_clan_name = str(existing_thread_data.get("clan_name") or "")
            if existing_clan_name and existing_clan_name != clan:
                return {"issue": f"That thread is already linked to {existing_clan_name}."}
            if existing_clan_name == clan:
                return {"issue": None, "status": "already", "thread": thread,
                        "clan": clan, "message": f"{clan} is already linked to this thread."}
        for existing_clan, config in self.clan_configs.items():
            if existing_clan != clan and config.get("thread_id") == thread.id:
                return {"issue": f"That thread is already linked to {existing_clan}."}
        prior_threads = sorted(
            str(existing_thread_id)
            for existing_thread_id, thread_data in threads.items()
            if str(existing_thread_id) != thread_key
            and isinstance(thread_data, dict)
            and thread_data.get("clan_name") == clan
        )
        return {"issue": None, "status": "new", "thread": thread,
                "clan": clan, "prior_threads": prior_threads,
                "prior_data": {thread_id: copy.deepcopy(threads[thread_id])
                               for thread_id in prior_threads},
                "existing_thread_data": copy.deepcopy(existing_thread_data),
                "clan_thread_id": self.clan_configs[clan].get("thread_id"),
                "message": f"This thread is now linked to {clan} CWL updates."}

    def cwl_registration_welcome_embed(self, clan: str) -> discord.Embed:
        return discord.Embed(
            title=f"CWL Thread Ready — {clan}",
            description="This thread will now receive CWL status updates.",
            color=discord.Color.green(),
        )

    async def cwl_registration_status_preview(self, clan: str):
        clan_code = CLAN_NAME_TO_CODE.get(clan)
        if clan_code is None:
            return {"kind": "none"}
        latest = await self._latest_thread_snapshot(clan_code, force_refresh=True)
        if latest is None:
            return {"kind": "unavailable"}
        _, snapshot = latest
        if not snapshot.has_active_round:
            return {"kind": "none"}
        status, _ = self._prepare_cc_state({}, snapshot.preparation)
        embed, _view = await self._build_thread_status_board(
            clan_code, snapshot, status,
        )
        return {"kind": "board", "embed": embed}

    async def apply_cwl_thread_registration(self,
                                            prepared: dict[str, object]) -> str:
        clan = prepared["clan"]
        thread = prepared["thread"]
        if prepared["status"] == "already":
            self.clan_configs[clan]["thread_id"] = thread.id
            return prepared["message"]
        threads = self.data.setdefault("threads", {})
        for existing_thread_id in prepared["prior_threads"]:
            self._drop_thread_registration(existing_thread_id)
        self.clan_configs[clan]["thread_id"] = thread.id
        threads[str(thread.id)] = {
            "clan_name": clan,
            "sticky_message_id": None,
            "stale_sticky_message_ids": [],
            "cc_status": {},
            "cc_statuses": {},
            "last_activity": self._utc_now_iso(),
        }
        self.save_data()
        await thread.send(embed=self.cwl_registration_welcome_embed(clan))
        clan_code = CLAN_NAME_TO_CODE.get(clan)
        if clan_code is not None:
            await self.refresh_registered_cwl_status_for_clan(clan_code)
        return prepared["message"]

    def has_leader_permissions(self, member: discord.Member) -> bool:
        """Check if member has any LEAD_PLUS role."""
        return any(role.id in self.lead_role_ids for role in member.roles)


    def has_helper_permissions(self, member: discord.Member) -> bool:
        """Check if member has any CWL_HELPERS role."""
        return any(role.id in self.helper_role_ids for role in member.roles)


    def check_permissions(self, interaction: discord.Interaction, require_leader: bool = False) -> bool:
        """Check if user has required permissions."""
        member = interaction.user
        if require_leader:
            if not self.has_leader_permissions(member):
                return False
        else:
            # For helper commands, check if they have either leader or helper permissions
            if not (self.has_leader_permissions(member) or self.has_helper_permissions(member)):
                return False
        return True


    @app_commands.choices(clan=THREAD_CLAN_CHOICES)
    @app_commands.describe(
        clan="Clan this CWL thread belongs to.",
        thread_id="Thread ID for the CWL discussion thread.",
    )
    async def register_cwl_thread(
        self,
        interaction: discord.Interaction,
        clan: str,
        thread_id: str,
    ) -> None:
        """Register a thread for CWL management."""
        if not self._has_any_role(interaction, CORE):
            await deny(interaction)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            prepared = await self.prepare_cwl_thread_registration(clan, thread_id)
            if prepared["issue"]:
                if prepared.get("invalid_id"):
                    await warn(interaction, prepared["issue"])
                else:
                    await interaction.followup.send(prepared["issue"], ephemeral=True)
                return
            message = await self.apply_cwl_thread_registration(prepared)
            await interaction.followup.send(message, ephemeral=True)

        except (discord.Forbidden, discord.HTTPException, ValueError, TypeError, RuntimeError) as e:
            LOGGER.exception(
                "register_cwl_thread failed: clan=%s thread_id=%s user=%s error=%s",
                clan,
                thread_id,
                getattr(interaction.user, "id", None),
                e,
            )
            await fail(interaction)

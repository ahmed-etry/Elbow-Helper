"""Thread feature command handlers and permission helpers."""

from __future__ import annotations

import logging

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
            try:
                thread_id_int = int(thread_id)
            except ValueError:
                await warn(
                    interaction,
                    "That thread ID isn't valid. It should be a number — you can get it from the thread URL.",
                )
                return

            thread = self.bot.get_channel(thread_id_int)
            # Cache can miss valid thread IDs after restart/shard changes; fetch is the API fallback.
            if thread is None:
                try:
                    thread = await self.bot.fetch_channel(thread_id_int)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    thread = None
            if not thread or not isinstance(thread, discord.Thread):
                await interaction.followup.send(
                    "I couldn't find that thread. Check the thread ID and try again.",
                    ephemeral=True,
                )
                return

            threads = self.data.setdefault("threads", {})
            thread_key = str(thread.id)
            existing_thread_data = threads.get(thread_key)
            if isinstance(existing_thread_data, dict):
                existing_clan_name = str(existing_thread_data.get("clan_name") or "")
                if existing_clan_name and existing_clan_name != clan:
                    await interaction.followup.send(
                        f"That thread is already linked to {existing_clan_name}.",
                        ephemeral=True,
                    )
                    return
                if existing_clan_name == clan:
                    self.clan_configs[clan]["thread_id"] = thread.id
                    await interaction.followup.send(
                        f"{clan} is already linked to this thread.",
                        ephemeral=True,
                    )
                    return

            for existing_clan, config in self.clan_configs.items():
                if existing_clan != clan and config.get("thread_id") == thread.id:
                    await interaction.followup.send(
                        f"That thread is already linked to {existing_clan}.",
                        ephemeral=True,
                    )
                    return

            # Retire any previous thread registrations for this clan.
            for existing_thread_id, thread_data in list(threads.items()):
                if str(existing_thread_id) == thread_key:
                    continue
                if isinstance(thread_data, dict) and thread_data.get("clan_name") == clan:
                    self._drop_thread_registration(str(existing_thread_id))

            self.clan_configs[clan]["thread_id"] = thread.id
            now_iso = self._utc_now_iso()
            threads[thread_key] = {
                "clan_name": clan,
                "sticky_message_id": None,
                "stale_sticky_message_ids": [],
                "cc_status": {},
                "cc_statuses": {},
                "last_activity": now_iso,
            }

            self.save_data()

            welcome_embed = discord.Embed(
                title=f"CWL Thread Ready — {clan}",
                description="This thread will now receive CWL status updates.",
                color=discord.Color.green(),
            )
            await thread.send(embed=welcome_embed)
            clan_code = CLAN_NAME_TO_CODE.get(clan)
            if clan_code is not None:
                await self.refresh_registered_cwl_status_for_clan(clan_code)

            await interaction.followup.send(
                f"This thread is now linked to {clan} CWL updates.",
                ephemeral=True,
            )

        except (discord.Forbidden, discord.HTTPException, ValueError, TypeError, RuntimeError) as e:
            LOGGER.exception(
                "register_cwl_thread failed: clan=%s thread_id=%s user=%s error=%s",
                clan,
                thread_id,
                getattr(interaction.user, "id", None),
                e,
            )
            await fail(interaction)

from __future__ import annotations

import logging
from datetime import datetime

import discord
from discord import app_commands
from elbow_helper.discord.interactions import deny

from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import (
    ALLIANCE_MEMBER_ROLE_ID,
    CO_LEADER_ROLE_ID,
    CROSS_LEADER_ROLE_ID,
    HIBERNATING_ROLE_ID,
    LEAD_PLUS,
    MEMBER_ROLE_ID,
    SLEEPING_CO_ROLE_ID,
    SLEEPING_CROSS_ROLE_ID,
)

from .config import (
    HIBERNATE_REMOVE_ROLE_IDS,
    RESTORE_ROLE_IDS,
    SNAPSHOT_ONLY_ROLE_IDS,
)
from .state import load_hibernation_state, save_hibernation_state

LOGGER = logging.getLogger(__name__)


class HibernationCommandMixin:
    async def _send_reactivation_reply(
        self,
        interaction: discord.Interaction,
        message: str,
        *,
        ephemeral: bool,
    ) -> None:
        if interaction.guild is None:
            await interaction.followup.send(message)
            return
        await interaction.followup.send(message, ephemeral=ephemeral)

    async def _complete_reactivation(
        self,
        interaction: discord.Interaction,
        *,
        guild: discord.Guild,
        actor: discord.Member | discord.User,
        target: discord.Member,
        force_reactivate: bool,
        response_ephemeral: bool,
    ) -> None:
        data = load_hibernation_state()

        if force_reactivate and not (
            isinstance(actor, discord.Member)
            and any(role.id in LEAD_PLUS for role in actor.roles)
        ):
            await self._send_reactivation_reply(
                interaction,
                "You don't have permission to reactivate another member.",
                ephemeral=response_ephemeral,
            )
            return

        if str(target.id) not in data:
            message = (
                f"{target.mention} is not currently hibernating."
                if force_reactivate
                else "You're not currently hibernating."
            )
            await self._send_reactivation_reply(
                interaction,
                message,
                ephemeral=response_ephemeral,
            )
            return

        hibernation_info = data[str(target.id)]

        to_add = [guild.get_role(MEMBER_ROLE_ID), guild.get_role(ALLIANCE_MEMBER_ROLE_ID)]
        to_remove = [guild.get_role(HIBERNATING_ROLE_ID)]

        if SLEEPING_CO_ROLE_ID in [role.id for role in target.roles]:
            to_add.append(guild.get_role(CO_LEADER_ROLE_ID))
            to_remove.append(guild.get_role(SLEEPING_CO_ROLE_ID))
        if SLEEPING_CROSS_ROLE_ID in [role.id for role in target.roles]:
            to_add.append(guild.get_role(CROSS_LEADER_ROLE_ID))
            to_remove.append(guild.get_role(SLEEPING_CROSS_ROLE_ID))

        for role_id in hibernation_info["roles"]:
            role = guild.get_role(role_id)
            if role is not None:
                to_add.append(role)

        to_add = [role for role in to_add if role is not None]
        to_remove = [role for role in to_remove if role is not None]

        if to_remove:
            await target.remove_roles(*to_remove, reason=f"Reactivated by {actor}")
        if to_add:
            await target.add_roles(*to_add, reason=f"Reactivated by {actor}")

        try:
            await self.achievement_rewards.track_hibernation_survivor(
                target.id
            )
        except (RuntimeError, TypeError):
            LOGGER.exception("Failed tracking hibernation survivor achievement")

        target = await guild.fetch_member(target.id)
        del data[str(target.id)]
        save_hibernation_state(data)

        await self._create_reactivation_ticket(
            guild=guild,
            actor=actor,
            target=target,
            hibernation_info=hibernation_info,
        )
        await self._archive_fallback_thread_for_member(
            target.id,
            reason=f"Hibernation ended by {actor}",
        )

        if force_reactivate:
            await self._send_reactivation_reply(
                interaction,
                f"Reactivated {target.mention}. Their roles were restored and a ticket was created.",
                ephemeral=response_ephemeral,
            )
        else:
            await self._send_reactivation_reply(
                interaction,
                "Welcome back. Your roles have been restored.",
                ephemeral=response_ephemeral,
            )

    async def reactivate_from_button(self, interaction: discord.Interaction) -> None:
        response_ephemeral = interaction.guild is not None
        if response_ephemeral:
            await interaction.response.defer(ephemeral=True, thinking=True)
        else:
            await interaction.response.defer(thinking=True)

        try:
            guild = interaction.guild
            if guild is None or guild.id != GUILD_ID:
                guild = self.bot.get_guild(GUILD_ID)

            if guild is None:
                await self._send_reactivation_reply(
                    interaction,
                    "The server is not available right now. Try again shortly.",
                    ephemeral=response_ephemeral,
                )
                return

            actor = interaction.user
            if isinstance(actor, discord.Member) and actor.guild.id == guild.id:
                target = actor
            else:
                target = guild.get_member(actor.id)
                if target is None:
                    try:
                        target = await guild.fetch_member(actor.id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        target = None

            if target is None:
                await self._send_reactivation_reply(
                    interaction,
                    "I couldn't find you in the server. Ask staff to complete your reactivation.",
                    ephemeral=response_ephemeral,
                )
                return

            await self._complete_reactivation(
                interaction,
                guild=guild,
                actor=actor,
                target=target,
                force_reactivate=False,
                response_ephemeral=response_ephemeral,
            )
        except (discord.Forbidden, discord.HTTPException, OSError, RuntimeError, TypeError, ValueError):
            LOGGER.exception("Button reactivation failed for actor %s", interaction.user.id)
            await self._send_reactivation_reply(
                interaction,
                "Reactivation couldn't be completed. Try again in a moment, or ask staff for help.",
                ephemeral=response_ephemeral,
            )

    @app_commands.command(name="hibernate", description="Move a member into hibernation while saving their roles.")
    @app_commands.describe(user="Member to move into hibernation.")
    @app_commands.default_permissions(manage_roles=True)
    async def hibernate_user(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not any(role.id in LEAD_PLUS for role in interaction.user.roles):
            await deny(interaction)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            guild = interaction.guild
            if guild is None:
                await interaction.followup.send("Run this command in the server, not in DMs.", ephemeral=True)
                return

            data = load_hibernation_state()
            if str(user.id) in data:
                await interaction.followup.send("That member is already hibernating.", ephemeral=True)
                return

            stored_role_ids = [
                role.id
                for role in user.roles
                if not role.is_default() and role.id not in SNAPSHOT_ONLY_ROLE_IDS
            ]
            snapshot_role_ids = [role.id for role in user.roles if role.id in SNAPSHOT_ONLY_ROLE_IDS]

            unix_ts = int(datetime.now().timestamp())
            data[str(user.id)] = {
                "roles": stored_role_ids,
                "rank_roles": snapshot_role_ids,
                "hibernation_date": f"<t:{unix_ts}:F>",
            }
            save_hibernation_state(data)

            to_remove = [
                guild.get_role(role_id)
                for role_id in HIBERNATE_REMOVE_ROLE_IDS
                if guild.get_role(role_id) in user.roles
            ]
            to_remove.extend(
                [
                    role
                    for role_id in (*RESTORE_ROLE_IDS, *SNAPSHOT_ONLY_ROLE_IDS)
                    if (role := guild.get_role(role_id)) in user.roles
                ]
            )
            to_add = [guild.get_role(HIBERNATING_ROLE_ID)]

            if guild.get_role(CO_LEADER_ROLE_ID) in user.roles:
                to_add.append(guild.get_role(SLEEPING_CO_ROLE_ID))
                to_remove.append(guild.get_role(CO_LEADER_ROLE_ID))
            if guild.get_role(CROSS_LEADER_ROLE_ID) in user.roles:
                to_add.append(guild.get_role(SLEEPING_CROSS_ROLE_ID))
                to_remove.append(guild.get_role(CROSS_LEADER_ROLE_ID))

            to_remove = [role for role in to_remove if role is not None]
            to_add = [role for role in to_add if role is not None]

            if to_remove:
                await user.remove_roles(*to_remove, reason=f"Moved to hibernation by {interaction.user}")
            if to_add:
                await user.add_roles(*to_add, reason=f"Moved to hibernation by {interaction.user}")

            await self._send_hibernation_log(interaction, user, stored_role_ids, snapshot_role_ids, unix_ts)
            await interaction.followup.send(f"Moved {user.mention} to hibernation.", ephemeral=True)
            await self._send_hibernation_notice(user)

        except (discord.Forbidden, discord.HTTPException, OSError, RuntimeError, TypeError, ValueError):
            LOGGER.exception("/hibernate failed for user %s", user.id)
            await interaction.followup.send(
                "I couldn't move that member into hibernation right now. Try again in a moment.",
                ephemeral=True,
            )

    @app_commands.command(name="reactivate", description="Return from hibernation and restore your saved roles.")
    @app_commands.describe(user="Leadership can choose another member. Leave empty to reactivate yourself.")
    async def reactivate(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            guild = interaction.guild
            actor = interaction.user
            target = user or actor

            if guild is None:
                await interaction.followup.send("Run this command in the server, not in DMs.", ephemeral=True)
                return

            force_reactivate = user is not None and user.id != actor.id
            await self._complete_reactivation(
                interaction,
                guild=guild,
                actor=actor,
                target=target,
                force_reactivate=force_reactivate,
                response_ephemeral=True,
            )

        except (discord.Forbidden, discord.HTTPException, OSError, RuntimeError, TypeError, ValueError):
            LOGGER.exception("/reactivate failed for actor %s", interaction.user.id)
            await interaction.followup.send(
                "Reactivation couldn't be completed. Try again in a moment, or ask staff for help.",
                ephemeral=True,
            )

from __future__ import annotations

import logging
from datetime import datetime

import discord
from discord import app_commands
from elbow_helper.infrastructure.discord.interactions import deny
from elbow_helper.configuration.channels import HIBERNATION_FALLBACK, HIBERNATION_LOG

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
from .state import get_fallback_thread_entry, load_hibernation_state, save_hibernation_member

LOGGER = logging.getLogger(__name__)


class HibernationCommandMixin:
    def prepare_hibernation(self, guild: discord.Guild,
                            user: discord.Member) -> dict[str, object]:
        info = self.hibernation_member_state(user.id)
        if info is not None and (
            "hibernation_progress" not in info or "reactivation_progress" in info
        ):
            return {"issue": "That member is already hibernating."}
        if info is not None:
            progress = info["hibernation_progress"]
            get_role = guild.get_role
            return {
                "issue": None, "user": user,
                "stored_role_ids": info["roles"],
                "snapshot_role_ids": info["rank_roles"],
                "unix_ts": progress["unix_ts"],
                "to_remove": tuple(get_role(role_id) for role_id in progress["to_remove"]
                                   if get_role(role_id) is not None),
                "to_add": tuple(get_role(role_id) for role_id in progress["to_add"]
                                if get_role(role_id) is not None),
                "missing_role_ids": tuple(role_id for role_id in progress["to_add"]
                                          if get_role(role_id) is None),
                "log_channel_id": HIBERNATION_LOG,
                "fallback_channel_id": HIBERNATION_FALLBACK,
            }
        stored_role_ids = [
            role.id for role in user.roles
            if not role.is_default() and role.id not in SNAPSHOT_ONLY_ROLE_IDS
        ]
        snapshot_role_ids = [role.id for role in user.roles
                             if role.id in SNAPSHOT_ONLY_ROLE_IDS]
        unix_ts = int(datetime.now().timestamp())
        get_role = getattr(guild, "get_role", lambda _: None)
        to_remove = [
            get_role(role_id)
            for role_id in HIBERNATE_REMOVE_ROLE_IDS
            if get_role(role_id) in user.roles
        ]
        to_remove.extend(
            role for role_id in (*RESTORE_ROLE_IDS, *SNAPSHOT_ONLY_ROLE_IDS)
            if (role := get_role(role_id)) in user.roles
        )
        to_add = [get_role(HIBERNATING_ROLE_ID)]
        required_ids = [HIBERNATING_ROLE_ID]
        if get_role(CO_LEADER_ROLE_ID) in user.roles:
            to_add.append(get_role(SLEEPING_CO_ROLE_ID))
            to_remove.append(get_role(CO_LEADER_ROLE_ID))
            required_ids.append(SLEEPING_CO_ROLE_ID)
        if get_role(CROSS_LEADER_ROLE_ID) in user.roles:
            to_add.append(get_role(SLEEPING_CROSS_ROLE_ID))
            to_remove.append(get_role(CROSS_LEADER_ROLE_ID))
            required_ids.append(SLEEPING_CROSS_ROLE_ID)
        return {
            "issue": None, "user": user,
            "stored_role_ids": stored_role_ids,
            "snapshot_role_ids": snapshot_role_ids,
            "unix_ts": unix_ts,
            "to_remove": tuple(role for role in to_remove if role is not None),
            "to_add": tuple(role for role in to_add if role is not None),
            "missing_role_ids": tuple(role_id for role_id in required_ids
                                      if get_role(role_id) is None),
            "log_channel_id": HIBERNATION_LOG,
            "fallback_channel_id": HIBERNATION_FALLBACK,
        }

    def hibernation_notice_preview(self, user: discord.Member) -> str:
        return self._build_hibernation_notice_message(user)

    def hibernation_member_state(self, member_id: int) -> dict[str, object] | None:
        state = load_hibernation_state().get(str(member_id))
        return dict(state) if isinstance(state, dict) else None

    def prepare_reactivation(self, *, guild: discord.Guild,
                             actor: discord.Member | discord.User,
                             target: discord.Member,
                             force_reactivate: bool) -> dict[str, object]:
        if force_reactivate and not (
            isinstance(actor, discord.Member)
            and any(role.id in LEAD_PLUS for role in actor.roles)
        ):
            return {"issue": "You don't have permission to reactivate another member."}
        info = self.hibernation_member_state(target.id)
        if info is None:
            message = (f"{target.mention} is not currently hibernating."
                       if force_reactivate else "You're not currently hibernating.")
            return {"issue": message}
        to_add = [guild.get_role(MEMBER_ROLE_ID), guild.get_role(ALLIANCE_MEMBER_ROLE_ID)]
        to_remove = [guild.get_role(HIBERNATING_ROLE_ID)]
        if SLEEPING_CO_ROLE_ID in [role.id for role in target.roles]:
            to_add.append(guild.get_role(CO_LEADER_ROLE_ID))
            to_remove.append(guild.get_role(SLEEPING_CO_ROLE_ID))
        if SLEEPING_CROSS_ROLE_ID in [role.id for role in target.roles]:
            to_add.append(guild.get_role(CROSS_LEADER_ROLE_ID))
            to_remove.append(guild.get_role(SLEEPING_CROSS_ROLE_ID))
        for role_id in info["roles"]:
            role = guild.get_role(role_id)
            if role is not None:
                to_add.append(role)
        to_add = tuple(role for role in to_add if role is not None)
        to_remove = tuple(role for role in to_remove if role is not None)
        progress = info.get("reactivation_progress")
        if progress is not None:
            to_add = tuple(guild.get_role(role_id) for role_id in progress["to_add"]
                           if guild.get_role(role_id) is not None)
            to_remove = tuple(guild.get_role(role_id) for role_id in progress["to_remove"]
                              if guild.get_role(role_id) is not None)
        removed_ids = {role.id for role in to_remove}
        projected_roles = [role for role in target.roles if role.id not in removed_ids]
        projected_ids = {role.id for role in projected_roles}
        projected_roles.extend(role for role in to_add if role.id not in projected_ids)
        ticket = self.prepare_reactivation_ticket(
            guild, actor, target, info, projected_roles=projected_roles,
        )
        fallback = get_fallback_thread_entry(load_hibernation_state(), target.id)
        return {"issue": None, "actor": actor, "target": target,
                "guild": guild, "info": info,
                "force_reactivate": force_reactivate,
                "to_add": to_add, "to_remove": to_remove,
                "missing_role_ids": tuple(role_id for role_id in info["roles"]
                                          if guild.get_role(role_id) is None),
                "ticket": ticket,
                "fallback_thread_id": fallback.get("thread_id") if fallback else None}

    async def _resume_hibernation_roles(self, guild, member, info, progress, *, reason):
        for operation in ("to_remove", "to_add"):
            while progress[operation]:
                role_id = progress[operation][0]
                role = guild.get_role(role_id)
                if role is None and operation == "to_add":
                    raise RuntimeError(f"Hibernation role {role_id} is unavailable")
                if role is not None:
                    change = member.remove_roles if operation == "to_remove" else member.add_roles
                    await change(role, reason=reason)
                progress[operation].pop(0)
                save_hibernation_member(member.id, info)

    @staticmethod
    def _hibernation_role_progress(plan):
        return {key: list(dict.fromkeys(role.id for role in plan[key]))
                for key in ("to_remove", "to_add")}

    async def reactivate_member(self, plan: dict[str, object]) -> dict[str, object]:
        guild = plan["guild"]
        actor = plan["actor"]
        target = plan["target"]
        info = self.hibernation_member_state(target.id)
        if info is None:
            raise RuntimeError("Hibernation state is unavailable")
        if "reactivation_progress" not in info:
            info["reactivation_progress"] = self._hibernation_role_progress(plan)
            save_hibernation_member(target.id, info)
        progress = info["reactivation_progress"]
        await self._resume_hibernation_roles(
            guild, target, info, progress, reason=f"Reactivated by {actor}",
        )
        if not progress.get("achievement_tracked"):
            await self.achievement_rewards.track_hibernation_survivor(target.id)
            progress["achievement_tracked"] = True
            save_hibernation_member(target.id, info)
        target = await guild.fetch_member(target.id)
        ticket = await self._create_reactivation_ticket(
            guild=guild, actor=actor, target=target,
            hibernation_info=info,
        )
        await self._archive_fallback_thread_for_member(
            target.id, reason=f"Hibernation ended by {actor}",
        )
        save_hibernation_member(target.id, None)
        message = (f"Reactivated {target.mention}. Their roles were restored and a ticket was created."
                   if plan["force_reactivate"] else
                   "Welcome back. Your roles have been restored.")
        return {"message": message, "ticket": ticket,
                "ticket_channel_id": ticket.id,
                "member_id": target.id}

    async def hibernate_member(self, guild: discord.Guild,
                               actor: discord.Member,
                               plan: dict[str, object],
                               *, reply=None) -> str:
        user = plan["user"]
        info = self.hibernation_member_state(user.id)
        if info is not None and (
            "hibernation_progress" not in info or "reactivation_progress" in info
        ):
            return "That member is already hibernating."
        if plan.get("missing_role_ids"):
            raise RuntimeError("Required hibernation roles are unavailable")
        if info is None:
            info = {
                "roles": list(plan["stored_role_ids"]),
                "rank_roles": list(plan["snapshot_role_ids"]),
                "hibernation_date": f"<t:{plan['unix_ts']}:F>",
                "hibernation_progress": {
                    **self._hibernation_role_progress(plan), "unix_ts": plan["unix_ts"],
                },
            }
            save_hibernation_member(user.id, info)
        progress = info["hibernation_progress"]
        await self._resume_hibernation_roles(
            guild, user, info, progress, reason=f"Moved to hibernation by {actor}",
        )
        if not progress.get("log_sent"):
            await self.send_hibernation_log(
                guild, actor, user, info["roles"], info["rank_roles"], progress["unix_ts"],
            )
            progress["log_sent"] = True
            save_hibernation_member(user.id, info)
        await self._send_hibernation_notice(user)
        del info["hibernation_progress"]
        save_hibernation_member(user.id, info)
        message = f"Moved {user.mention} to hibernation."
        if reply is not None:
            await reply(message)
        return message

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
        self, interaction: discord.Interaction, *,
        guild: discord.Guild, actor: discord.Member | discord.User,
        target: discord.Member, force_reactivate: bool,
        response_ephemeral: bool,
    ) -> None:
        plan = self.prepare_reactivation(
            guild=guild, actor=actor, target=target,
            force_reactivate=force_reactivate,
        )
        if plan["issue"]:
            await self._send_reactivation_reply(
                interaction, plan["issue"], ephemeral=response_ephemeral,
            )
            return
        result = await self.reactivate_member(plan)
        await self._send_reactivation_reply(
            interaction, result["message"], ephemeral=response_ephemeral,
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

            plan = self.prepare_hibernation(guild, user)
            if plan["issue"]:
                await interaction.followup.send(plan["issue"], ephemeral=True)
                return
            await self.hibernate_member(
                guild, interaction.user, plan,
                reply=lambda message: interaction.followup.send(
                    message, ephemeral=True,
                ),
            )

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

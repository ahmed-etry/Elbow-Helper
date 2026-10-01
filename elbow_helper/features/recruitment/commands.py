"""Slash commands for recruitment workflows."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
import sqlite3
from typing import Optional

import discord
from discord import app_commands
from elbow_helper.discord.interactions import deny
from elbow_helper.discord.interactions import fail
from elbow_helper.discord.interactions import warn
from elbow_helper.configuration.channels import GET_STARTED_CHANNEL
from elbow_helper.configuration.channels import RECRUITMENT_TICKET_CATEGORY
from elbow_helper.configuration.channels import SELF_ROLES
from elbow_helper.configuration.channels import SERVER_RULES
from elbow_helper.configuration.clans import CLAN_INFO_BOARDS
from elbow_helper.configuration.roles import AGE_ROLE_IDS
from elbow_helper.configuration.roles import ALLIANCE_MEMBER_ROLE_ID
from elbow_helper.configuration.roles import APPLICANT_ROLE_ID
from elbow_helper.configuration.roles import CORE
from elbow_helper.configuration.roles import LEAD
from elbow_helper.configuration.roles import MEMBER_ROLE_ID
from elbow_helper.configuration.roles import REGION_ROLE_IDS
from elbow_helper.configuration.roles import RECRUITERS
from elbow_helper.configuration.roles import TRIAL_ROLE_ID

from .config import DECLINED_TICKET_PREFIXES
from .config import TRIAL_DAYS_DEFAULT
from .helpers import can_rename
from .helpers import rename_ticket_channel
from .statements import ACCEPT_TEMPLATE
from .statements import CHECKUP_TEMPLATE
from .statements import DECLINE_TEMPLATE
from .statements import FINALIZE_TEMPLATE
from .statements import RARE_STATEMENTS
from .trials import TrialStartResult
from .views import AcceptConfirmationView

RARE_STATEMENT_META = {
    "under16": {"name": "Under 16", "emoji": "🔞"},
    "hyperactive": {"name": "Hyperactive", "emoji": "⚡"},
}

RARE_STATEMENT_CHOICES = [
    app_commands.Choice(name=statement["name"], value=template_key)
    for template_key, statement in RARE_STATEMENT_META.items()
]


class RecruitmentCommandMixin:
    def prepare_finalize(self, applicant: discord.Member,
                         channel: discord.TextChannel | None,
                         guild: discord.Guild,
                         additional_notes: str | None = None) -> dict[str, object]:
        if not isinstance(channel, discord.TextChannel):
            return {"issue": "Run this command in a server text channel."}
        current_name = channel.name
        new_name = (current_name.replace("🤔", "✅")
                    if "🤔" in current_name else current_name)
        trial_role = guild.get_role(TRIAL_ROLE_ID) if TRIAL_ROLE_ID else None
        member_role = guild.get_role(MEMBER_ROLE_ID) if MEMBER_ROLE_ID else None
        alliance_role = (guild.get_role(ALLIANCE_MEMBER_ROLE_ID)
                         if ALLIANCE_MEMBER_ROLE_ID else None)
        remove_roles = (trial_role,) if trial_role and trial_role in applicant.roles else ()
        add_roles = tuple(role for role in (member_role, alliance_role)
                          if role and role not in applicant.roles)
        notes = f"\n\n**Additional Notes:**\n{additional_notes}" if additional_notes else ""
        message = FINALIZE_TEMPLATE.format(
            user_mention=applicant.mention, additional_notes=notes,
        )
        return {"issue": None, "applicant": applicant, "channel": channel,
                "old_name": current_name, "new_name": new_name,
                "remove_roles": remove_roles, "add_roles": add_roles,
                "message": message}

    async def post_finalize(self, prepared: dict[str, object]) -> str:
        applicant = prepared["applicant"]
        channel = prepared["channel"]
        new_name = prepared["new_name"]
        name_changed = False
        rename_failed = False
        try:
            if new_name != channel.name:
                await channel.edit(name=new_name)
                name_changed = True
        except discord.Forbidden:
            rename_failed = True
        roles_changed: list[str] = []
        role_update_failed = False
        try:
            for role in prepared["remove_roles"]:
                await applicant.remove_roles(role)
                roles_changed.append(f"Removed {role.name}")
            for role in prepared["add_roles"]:
                await applicant.add_roles(role)
                roles_changed.append(f"Added {role.name}")
        except discord.Forbidden:
            role_update_failed = True
        await channel.send(prepared["message"])
        lines = [f"Completed {applicant.display_name}'s trial."]
        if name_changed:
            lines.append(f"Renamed the channel to {new_name}.")
        if roles_changed:
            lines.append(f"Updated roles: {', '.join(roles_changed)}.")
        lines.append(f"Sent to {channel.mention}.")
        if rename_failed:
            lines.append("Couldn't rename the channel — check permissions.")
        if role_update_failed:
            lines.append("Couldn't update roles — check permissions.")
        return "\n".join(lines)

    def prepare_decline(self, applicant: discord.Member,
                        channel: discord.TextChannel | None,
                        additional_notes: str | None = None) -> dict[str, object]:
        if not isinstance(channel, discord.TextChannel):
            return {"issue": "Run this command in a server text channel."}
        notes = f"\n\n**Additional Notes:** {additional_notes}" if additional_notes else ""
        message = DECLINE_TEMPLATE.format(
            user_mention=applicant.mention, additional_notes=notes,
        )
        rename_candidate = rename_ticket_channel(channel, DECLINED_TICKET_PREFIXES)
        return {"issue": None, "applicant": applicant, "channel": channel,
                "message": message, "rename_candidate": rename_candidate}

    async def post_decline(self, prepared: dict[str, object]) -> str:
        applicant = prepared["applicant"]
        channel = prepared["channel"]
        candidate = prepared["rename_candidate"]
        rename_failed = False
        if candidate and candidate != channel.name:
            if len(candidate) > 100:
                rename_failed = True
            elif can_rename(channel.guild.id):
                try:
                    await channel.edit(name=candidate)
                except (discord.Forbidden, discord.HTTPException):
                    rename_failed = True
            else:
                rename_failed = True
        await channel.send(prepared["message"])
        lines = [f"Declined {applicant.display_name}. Message sent to {channel.mention}."]
        if rename_failed:
            lines.append("Couldn't rename the channel — check permissions.")
        return "\n".join(lines)

    def prepare_checkup(self, applicant: discord.Member, account_linked: bool,
                        channel: discord.TextChannel | None,
                        additional_notes: str | None = None) -> dict[str, object]:
        has_age_role = self._member_has_any_role(applicant, AGE_ROLE_IDS)
        has_region_role = self._member_has_any_role(applicant, REGION_ROLE_IDS)
        has_required_self_roles = has_age_role and has_region_role
        account_link_line = ""
        self_roles_line = ""
        if not account_linked and not has_required_self_roles:
            account_link_line = (
                f"Also, please link all of your accounts in <#{GET_STARTED_CHANNEL}> "
                f"and choose all of the relevant roles for you in <#{SELF_ROLES}>.\n"
            )
        elif not account_linked:
            account_link_line = f"Also, please link all of your accounts in <#{GET_STARTED_CHANNEL}>.\n"
        elif not has_required_self_roles:
            self_roles_line = f"Also, please choose all of the relevant roles for you in <#{SELF_ROLES}>.\n"

        if account_linked and has_required_self_roles:
            followup_question = "Once you've read them, what questions do you have about the rules or expectations?"
        else:
            followup_question = (
                "Once you've read the rules and completed the setup above, what questions do you have about "
                "the rules or expectations?"
            )
        additional_notes_block = f"\n\n**Additional Notes:** {additional_notes}" if additional_notes else ""
        message = CHECKUP_TEMPLATE.format(
            user_mention=applicant.mention,
            server_rules=SERVER_RULES,
            account_link_line=account_link_line,
            self_roles_line=self_roles_line,
            followup_question=followup_question,
            additional_notes=additional_notes_block,
        )
        if not isinstance(channel, discord.TextChannel):
            return {"issue": "Run this command in a server text channel."}
        confirmation = (
            f"Recruitment conversation started with {applicant.display_name} in {channel.mention}.\n"
            f"All Clash accounts linked: {'Yes' if account_linked else 'No'} • "
            f"Age and region roles selected: {'Yes' if has_required_self_roles else 'No'}"
        )
        return {"issue": None, "channel": channel, "message": message,
                "confirmation": confirmation}

    async def post_checkup(self, prepared: dict[str, object]) -> str:
        await prepared["channel"].send(prepared["message"])
        return str(prepared["confirmation"])

    def prepare_recstatement(self, template_key: str,
                             applicant: discord.Member,
                             channel: discord.TextChannel,
                             additional_notes: str | None = None) -> dict[str, object]:
        if not isinstance(channel, discord.TextChannel):
            return {"issue": "Run this command in a server text channel."}
        statement = RARE_STATEMENTS.get(template_key)
        statement_meta = RARE_STATEMENT_META.get(template_key)
        if not statement or not statement_meta:
            return {"issue": "That recruitment message is no longer available. Choose another message from the list."}
        statement_text = statement.format(user=applicant.mention)
        if additional_notes:
            statement_text += f"\n\n**Additional Notes:** {additional_notes}"
        return {"issue": None, "channel": channel,
                "message": statement_text, "name": statement_meta["name"],
                "applicant": applicant}

    async def post_recstatement(self, prepared: dict[str, object]) -> str:
        channel = prepared["channel"]
        await channel.send(prepared["message"])
        return (f"Sent **{prepared['name']}** to {prepared['applicant'].display_name} "
                f"in {channel.mention}.")

    @staticmethod
    def _member_has_any_role(member: discord.Member, role_ids: frozenset[int]) -> bool:
        return any(role.id in role_ids for role in member.roles)

    def _build_accept_confirmation_embed(
        self,
        *,
        user: discord.Member,
        valid_clans: list[str],
        nickname: str,
        days: int,
        target_channel: discord.TextChannel,
        player_rows: list[dict[str, str]],
        additional_notes: str | None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title="Confirm Applicant Acceptance",
            color=discord.Color.blurple(),
        )
        trial_length = f"{days} day" if days == 1 else f"{days} days"
        embed.add_field(name="Applicant", value=user.mention, inline=False)
        embed.add_field(name="Nickname", value=nickname, inline=True)
        embed.add_field(name="Trial Length", value=trial_length, inline=True)
        embed.add_field(name="Clans", value=", ".join(valid_clans), inline=False)
        embed.add_field(name="Ticket", value=target_channel.mention, inline=False)
        tag_lines = [f"- {row['player_name']} (`{row['player_tag']}`)" for row in player_rows]
        embed.add_field(name="Player Tags", value="\n".join(tag_lines), inline=False)
        if additional_notes:
            embed.add_field(name="Additional Notes", value=additional_notes, inline=False)
        return embed

    async def prepare_accept(self, *, user: discord.Member, clans: str,
                             nickname: str, player_tags: str, days: int,
                             channel: discord.TextChannel | None,
                             additional_notes: str | None = None) -> dict[str, object]:
        if days < 1:
            return {"issue": "Enter a trial length of at least 1 day.", "warnings": ()}
        valid_clans, invalid_clans = self.parse_clan_input(clans)
        valid_tags, invalid_tags = self.parse_player_tag_input(player_tags)
        if not valid_clans:
            return {"issue": "None of those clan codes were recognized. Check the codes and try again.",
                    "warnings": ()}
        if not valid_tags:
            return {"issue": "None of those player tags were recognized. Check the tags and try again.",
                    "warnings": ()}
        if len(valid_clans) > 7:
            return {"issue": "Too many clans — maximum is 7.", "warnings": ()}
        warnings = []
        if invalid_clans:
            warnings.append(
                f"Some codes weren't recognized: {', '.join(invalid_clans)}. "
                f"Continuing with: {', '.join(valid_clans)}."
            )
        if invalid_tags:
            warnings.append(
                f"Some player tags weren't recognized: {', '.join(invalid_tags)}. "
                f"Continuing with: {', '.join(valid_tags)}."
            )
        missing_clans = [code for code in valid_clans if code not in CLAN_INFO_BOARDS]
        if missing_clans:
            return {"issue": (
                f"Recruitment info boards haven't been set up for: {', '.join(missing_clans)}. "
                "Check the recruitment setup."
            ), "warnings": tuple(warnings)}
        info_channels = []
        missing_channels = []
        for code in valid_clans:
            configured = self.bot.get_channel(CLAN_INFO_BOARDS[code]["channel_id"])
            if configured is None:
                missing_channels.append(code)
            else:
                info_channels.append(configured.mention)
        if missing_channels:
            return {"issue": (
                f"Recruitment info channels couldn't be found for: {', '.join(missing_channels)}. "
                "Check the recruitment setup."
            ), "warnings": tuple(warnings)}
        if not isinstance(channel, discord.TextChannel):
            return {"issue": "Run this command in a server text channel.",
                    "warnings": tuple(warnings)}
        player_rows = await self.account_links.lookup_players(valid_tags)
        clan_links = (CLAN_INFO_BOARDS[valid_clans[0]]["link"]
                      if len(valid_clans) == 1 else
                      "".join(f"**{code}:** {CLAN_INFO_BOARDS[code]['link']}\n"
                              for code in valid_clans))
        notes = f"\n\n**Additional Notes:** {additional_notes}" if additional_notes else ""
        welcome = ACCEPT_TEMPLATE.format(
            user_mention=user.mention,
            trial_length=f"{days} day" if days == 1 else f"{days} days",
            info_channels_text="/".join(info_channels),
            server_rules=SERVER_RULES, clan_links=clan_links,
            additional_notes=notes,
        )
        embed = self._build_accept_confirmation_embed(
            user=user, valid_clans=valid_clans, nickname=nickname,
            days=days, target_channel=channel, player_rows=player_rows,
            additional_notes=additional_notes,
        )
        return {"issue": None, "warnings": tuple(warnings),
                "user": user, "channel": channel,
                "valid_clans": tuple(valid_clans), "player_tags": tuple(valid_tags),
                "player_rows": tuple(player_rows), "nickname": nickname,
                "days": days, "additional_notes": additional_notes,
                "welcome": welcome, "embed": embed}

    def accept_effects(self, prepared: dict[str, object]) -> dict[str, object]:
        user = prepared["user"]
        guild = user.guild
        applicant_role = guild.get_role(APPLICANT_ROLE_ID)
        trial_role = guild.get_role(TRIAL_ROLE_ID)
        remove_roles = (applicant_role,) if applicant_role in user.roles else ()
        add_roles = []
        missing_roles = []
        if trial_role is None:
            missing_roles.append(TRIAL_ROLE_ID)
        elif trial_role not in user.roles:
            add_roles.append(trial_role)
        for code in prepared["valid_clans"]:
            role_id = CLAN_INFO_BOARDS[code]["clan_role"]
            role = guild.get_role(role_id)
            if role is None:
                missing_roles.append(role_id)
            elif role not in user.roles:
                add_roles.append(role)
        tags = tuple(str(row["player_tag"]) for row in prepared["player_rows"])
        links = self.account_links.get_links_by_tags(tags)
        return {"nickname_before": user.nick, "nickname_after": prepared["nickname"],
                "remove_roles": remove_roles, "add_roles": tuple(add_roles),
                "missing_roles": tuple(missing_roles), "links_before": links,
                "linked_tags": tags}

    async def _apply_accept_member_update(
        self,
        action: Callable[[], Awaitable[object]],
        *,
        label: str,
        user_id: int,
    ) -> bool:
        try:
            await action()
            return True
        except (discord.Forbidden, discord.HTTPException) as error:
            self.logger.warning(
                "%s failed during /accept: user_id=%s error=%s",
                label,
                user_id,
                error,
            )
            return False

    async def complete_accept_confirmation(
        self,
        interaction: discord.Interaction,
        payload: dict[str, object],
    ) -> None:
        user = interaction.guild.get_member(int(payload["user_id"])) if interaction.guild else None
        if not isinstance(user, discord.Member):
            await warn(interaction, "That applicant is no longer in the server.")
            return

        target_channel = self.bot.get_channel(int(payload["channel_id"])) if payload.get("channel_id") else interaction.channel
        if not isinstance(target_channel, discord.TextChannel):
            await warn(interaction, "Run this command in a server text channel.")
            return

        await self._perform_accept_flow(
            interaction,
            user=user,
            valid_clans=[str(code) for code in payload["valid_clans"]],
            nickname=str(payload["nickname"]),
            days=int(payload["days"]),
            target_channel=target_channel,
            additional_notes=str(payload["additional_notes"]) if payload.get("additional_notes") else None,
            player_tags=[str(tag) for tag in payload["player_tags"]],
        )

    async def perform_accept(
        self, *, user: discord.Member, valid_clans: list[str],
        nickname: str, days: int, target_channel: discord.TextChannel,
        additional_notes: str | None, player_tags: list[str],
        player_rows: list[dict[str, str]] | None = None,
    ) -> dict[str, object]:
        if days < 1:
            return {"issue": "Enter a trial length of at least 1 day.", "failures": ()}
        missing_clans = [code for code in valid_clans if code not in CLAN_INFO_BOARDS]
        if missing_clans:
            return {"issue": (
                f"Recruitment info boards haven't been set up for: {', '.join(missing_clans)}. "
                "Check the recruitment setup."
            ), "failures": ()}
        info_channels = []
        missing_channels = []
        for code in valid_clans:
            channel = self.bot.get_channel(CLAN_INFO_BOARDS[code]["channel_id"])
            if channel is None:
                missing_channels.append(code)
            else:
                info_channels.append(channel.mention)
        if missing_channels:
            return {"issue": (
                f"Recruitment info channels couldn't be found for: {', '.join(missing_channels)}. "
                "Check the recruitment setup."
            ), "failures": ()}
        clan_links = (CLAN_INFO_BOARDS[valid_clans[0]]["link"]
                      if len(valid_clans) == 1 else
                      "".join(f"**{code}:** {CLAN_INFO_BOARDS[code]['link']}\n"
                              for code in valid_clans))
        notes = f"\n\n**Additional Notes:** {additional_notes}" if additional_notes else ""
        welcome_msg = ACCEPT_TEMPLATE.format(
            user_mention=user.mention,
            trial_length=f"{days} day" if days == 1 else f"{days} days",
            info_channels_text="/".join(info_channels),
            server_rules=SERVER_RULES, clan_links=clan_links,
            additional_notes=notes,
        )
        failures: list[str] = []

        nickname_updated = await self._apply_accept_member_update(
            lambda: user.edit(nick=nickname),
            label="Nickname update",
            user_id=user.id,
        )
        if not nickname_updated:
            failures.append("Nickname was not changed.")

        guild = user.guild

        applicant_role = guild.get_role(APPLICANT_ROLE_ID)
        if applicant_role is not None and applicant_role in user.roles:
            removed = await self._apply_accept_member_update(
                lambda: user.remove_roles(applicant_role),
                label="Applicant role removal",
                user_id=user.id,
            )
            if not removed:
                failures.append("Applicant role was not removed.")

        trial_role = guild.get_role(TRIAL_ROLE_ID)
        if trial_role is None:
            failures.append("Trial role was not added.")
        elif trial_role not in user.roles:
            added = await self._apply_accept_member_update(
                lambda: user.add_roles(trial_role),
                label="Trial role addition",
                user_id=user.id,
            )
            if not added:
                failures.append("Trial role was not added.")

        for clan_code in valid_clans:
            clan_role_id = CLAN_INFO_BOARDS[clan_code]["clan_role"]
            clan_role = guild.get_role(clan_role_id)
            if clan_role is None:
                failures.append(f"{clan_code} role was not added.")
                continue
            if clan_role in user.roles:
                continue
            added = await self._apply_accept_member_update(
                lambda role=clan_role: user.add_roles(role),
                label=f"{clan_code} role addition",
                user_id=user.id,
            )
            if not added:
                failures.append(f"{clan_code} role was not added.")

        failed_tags: list[str] = []
        if player_rows is None:
            try:
                player_rows = await self.account_links.lookup_players(player_tags)
            except (OSError, RuntimeError):
                self.logger.exception(
                    "Player lookup failed during /accept for user_id=%s",
                    user.id,
                )
                player_rows = []
                failed_tags.extend(player_tags)

        for index, row in enumerate(player_rows):
            tag = str(row["player_tag"])
            try:
                self.account_links.upsert_link(
                    player_tag=tag,
                    discord_user_id=user.id,
                    is_primary=index == 0,
                    player_name_last_seen=str(row["player_name"]),
                )
            except (OSError, sqlite3.Error):
                self.logger.exception(
                    "Account link failed during /accept: user_id=%s player_tag=%s",
                    user.id,
                    tag,
                )
                failed_tags.append(tag)
        if failed_tags:
            failures.append(
                "Clash accounts were not linked: "
                + ", ".join(f"`{tag}`" for tag in failed_tags)
                + "."
            )

        try:
            await self.account_links.refresh_linked_boards()
        except (
            discord.Forbidden,
            discord.HTTPException,
            OSError,
            RuntimeError,
        ):
            self.logger.exception(
                "Failed refreshing missing-elder boards after /accept for user_id=%s",
                user.id,
            )

        try:
            await target_channel.send(welcome_msg)
        except (discord.Forbidden, discord.HTTPException):
            self.logger.exception(
                "Acceptance message failed during /accept for user_id=%s channel_id=%s",
                user.id,
                target_channel.id,
            )
            failures.append("Welcome message was not posted.")

        try:
            trial_result = await self.start_trial_for_accept(
                target_channel,
                days,
                user.id,
            )
        except (
            discord.Forbidden,
            discord.HTTPException,
            OSError,
            RuntimeError,
        ):
            self.logger.exception(
                "Trial start failed during /accept for user_id=%s channel_id=%s",
                user.id,
                target_channel.id,
            )
            trial_result = TrialStartResult(started=False)
        if not trial_result.started:
            failures.append("Trial tracking was not started.")
        elif not trial_result.ticket_renamed:
            failures.append("Ticket was not renamed for the trial.")

        try:
            await self.achievement_rewards.award_achievement(
                user.id,
                "fresh_recruit",
            )
        except (OSError, sqlite3.Error):
            self.logger.exception(
                "Fresh Recruit award failed during /accept for user_id=%s",
                user.id,
            )
            failures.append("Fresh Recruit achievement was not awarded.")
        else:
            self.logger.info(
                "Fresh Recruit award requested for user_id=%s",
                user.id,
            )

        if failures:
            lines = [
                f"Acceptance is incomplete for {user.mention}:",
                *(f"- {failure}" for failure in failures),
                "",
                "Other acceptance steps were completed.",
            ]
            return {"issue": None, "failures": tuple(failures),
                    "message": "\n".join(lines)}
        return {"issue": None, "failures": (), "message": None}

    async def _perform_accept_flow(
        self, interaction: discord.Interaction, *, user: discord.Member,
        valid_clans: list[str], nickname: str, days: int,
        target_channel: discord.TextChannel,
        additional_notes: str | None, player_tags: list[str],
    ) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except (discord.InteractionResponded, discord.NotFound):
            pass
        try:
            result = await self.perform_accept(
                user=user, valid_clans=valid_clans, nickname=nickname,
                days=days, target_channel=target_channel,
                additional_notes=additional_notes, player_tags=player_tags,
            )
            if result["issue"]:
                await warn(interaction, result["issue"])
            elif result["message"]:
                await warn(interaction, result["message"])
        except (
            discord.Forbidden, discord.HTTPException, OSError,
            RuntimeError, TypeError, ValueError, sqlite3.Error,
        ):
            self.logger.exception(
                "accept_applicant failed: invoker=%s target=%s",
                interaction.user.id, user.id,
            )
            await fail(interaction)

    @app_commands.command(
        name="accept",
        description="Accept an applicant, link their accounts, and start their trial."
    )
    @app_commands.describe(
        applicant="Applicant to accept.",
        clans="Clan codes to offer.",
        nickname="Nickname to set when you accept them.",
        player_tags="Player tags to link.",
        days="Trial length in days.",
        channel="Applicant ticket to post in. Leave empty to use this channel.",
        additional_notes="Extra note to include in the acceptance message."
    )
    async def accept_applicant(
        self,
        interaction: discord.Interaction,
        applicant: discord.Member,
        clans: str,
        nickname: str,
        player_tags: str,
        days: int = TRIAL_DAYS_DEFAULT,
        channel: Optional[discord.TextChannel] = None,
        additional_notes: str = None
    ):
        """Accept an applicant, link their accounts, and start their trial."""

        if not any(role.id in (LEAD | RECRUITERS) for role in interaction.user.roles):
            await deny(interaction)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            prepared = await self.prepare_accept(
                user=applicant, clans=clans, nickname=nickname,
                player_tags=player_tags, days=days,
                channel=channel or interaction.channel,
                additional_notes=additional_notes,
            )
            for warning in prepared["warnings"]:
                await interaction.followup.send(warning, ephemeral=True)
            if prepared["issue"]:
                await warn(interaction, prepared["issue"])
                return
            view = AcceptConfirmationView(
                self,
                payload={
                    "user_id": applicant.id,
                    "valid_clans": list(prepared["valid_clans"]),
                    "nickname": nickname,
                    "player_tags": list(prepared["player_tags"]),
                    "days": days,
                    "channel_id": prepared["channel"].id,
                    "additional_notes": additional_notes,
                },
            )
            message = await interaction.followup.send(
                embed=prepared["embed"], view=view, ephemeral=True, wait=True,
            )
            view.bind_message(message)

        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
            self.logger.exception(
                "accept_applicant failed: invoker=%s target=%s",
                interaction.user.id,
                applicant.id,
            )
            await fail(interaction)

    async def opinion_ticket_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        member = interaction.user
        guild = interaction.guild
        if (
            guild is None
            or not isinstance(member, discord.Member)
            or not self._member_has_any_role(member, CORE | RECRUITERS)
        ):
            return []

        query = (current or "").strip().removeprefix("#").casefold()
        choices: list[app_commands.Choice[str]] = []
        for channel in guild.text_channels:
            if channel.category_id != RECRUITMENT_TICKET_CATEGORY:
                continue
            if not channel.permissions_for(member).view_channel:
                continue
            if query and query not in channel.name.casefold():
                continue
            choices.append(
                app_commands.Choice(
                    name=f"#{channel.name}"[:100],
                    value=str(channel.id),
                )
            )
            if len(choices) == 25:
                break
        return choices

    @staticmethod
    def resolve_opinion_ticket(
        guild: discord.Guild | None,
        member: discord.Member,
        ticket: str,
    ) -> discord.TextChannel | None:
        if guild is None:
            return None
        try:
            channel_id = int(ticket)
        except (TypeError, ValueError):
            return None

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return None
        if channel.category_id != RECRUITMENT_TICKET_CATEGORY:
            return None
        if not channel.permissions_for(member).view_channel:
            return None
        return channel

    @staticmethod
    def _resolve_opinion_ticket(
        interaction: discord.Interaction,
        ticket: str,
    ) -> discord.TextChannel | None:
        return RecruitmentCommandMixin.resolve_opinion_ticket(
            interaction.guild, interaction.user, ticket,
        )

    @app_commands.command(name="opinion", description="Get an AI second opinion on an applicant ticket.")
    @app_commands.autocomplete(ticket=opinion_ticket_autocomplete)
    @app_commands.describe(ticket="Applicant ticket containing the original application and conversation.")
    @app_commands.checks.has_any_role(*(CORE | RECRUITERS))
    async def slash_opinion(self, interaction: discord.Interaction, ticket: str):
        await interaction.response.defer(ephemeral=True)

        ticket_channel = self._resolve_opinion_ticket(interaction, ticket)
        if ticket_channel is None:
            await interaction.followup.send(
                "Choose an applicant ticket.",
                ephemeral=True,
            )
            return

        try:
            opinion_result = await self.build_ticket_second_opinion(ticket_channel)
            if opinion_result is None:
                await interaction.followup.send("This ticket has no messages to use for a second opinion.", ephemeral=True)
                return

            for chunk in opinion_result:
                await interaction.followup.send(chunk, ephemeral=True)

        except RuntimeError as exc:
            self.logger.warning(
                "slash_opinion unavailable: invoker=%s ticket_channel=%s error=%s",
                interaction.user.id,
                ticket_channel.id,
                exc,
            )
            await interaction.followup.send("I couldn't generate an AI second opinion for this ticket.", ephemeral=True)
        except (discord.Forbidden, discord.HTTPException, TypeError, ValueError):
            self.logger.exception(
                "slash_opinion failed: invoker=%s ticket_channel=%s",
                interaction.user.id,
                ticket_channel.id,
            )
            await fail(interaction)

    @app_commands.command(
        name="decline",
        description="Decline an applicant and send the decision."
    )
    @app_commands.describe(
        applicant="Applicant to decline.",
        channel="Applicant ticket to post in. Leave empty to use this channel.",
        additional_notes="Reason or extra note to include in the decline message."
    )
    async def decline_applicant(
        self,
        interaction: discord.Interaction,
        applicant: discord.Member,
        channel: Optional[discord.TextChannel] = None,
        additional_notes: str = None
    ):
        """Decline an applicant and send the decision."""
        
        if not any(role.id in (CORE | RECRUITERS) for role in interaction.user.roles):
            await deny(interaction)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            prepared = self.prepare_decline(
                applicant, channel or interaction.channel, additional_notes,
            )
            if prepared["issue"]:
                await interaction.followup.send(prepared["issue"], ephemeral=True)
                return
            confirmation = await self.post_decline(prepared)
            await interaction.followup.send(confirmation, ephemeral=True)

        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
            self.logger.exception(
                "decline_applicant failed: invoker=%s target=%s channel=%s",
                interaction.user.id,
                applicant.id,
                getattr(channel or interaction.channel, "id", None),
            )
            await fail(interaction)

    @app_commands.command(
        name="checkup",
        description="Start the recruitment conversation with an applicant and include any remaining setup steps."
    )
    @app_commands.describe(
        applicant="Applicant to start the recruitment conversation with.",
        account_linked="Has the applicant linked all of their Clash accounts?",
        channel="Applicant ticket to post in. Leave empty to use this channel.",
        additional_notes="Extra note to include in the recruitment message."
    )
    async def send_checkup(
        self,
        interaction: discord.Interaction,
        applicant: discord.Member,
        account_linked: bool,
        channel: Optional[discord.TextChannel] = None,
        additional_notes: str = None
    ):
        """Send a checkup message to an applicant"""
        
        if not any(role.id in (CORE | RECRUITERS) for role in interaction.user.roles):
            await deny(interaction)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            prepared = self.prepare_checkup(
                applicant, account_linked, channel or interaction.channel,
                additional_notes,
            )
            if prepared["issue"]:
                await interaction.followup.send(prepared["issue"], ephemeral=True)
                return
            confirmation_msg = await self.post_checkup(prepared)
            await interaction.followup.send(confirmation_msg, ephemeral=True)
            
        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
            self.logger.exception(
                "send_checkup failed: invoker=%s target=%s channel=%s",
                interaction.user.id,
                applicant.id,
                getattr(channel or interaction.channel, "id", None),
            )
            await fail(interaction)

    @app_commands.command(
        name="finalize",
        description="End a recruit's trial and give them full member access."
    )
    @app_commands.describe(
        applicant="Recruit whose trial is ending.",
        channel="Trial ticket to update. Leave empty to use this channel.",
        additional_notes="Extra note to include in the trial-ending message."
    )
    async def finalize_applicant(
        self,
        interaction: discord.Interaction,
        applicant: discord.Member,
        channel: Optional[discord.TextChannel] = None,
        additional_notes: Optional[str] = None
    ):
        """Finalize an applicant's trial and update channel status"""
        
        if not any(role.id in (CORE | RECRUITERS) for role in interaction.user.roles):
            await deny(interaction)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            prepared = self.prepare_finalize(
                applicant, channel or interaction.channel,
                interaction.guild, additional_notes,
            )
            if prepared["issue"]:
                await interaction.followup.send(prepared["issue"], ephemeral=True)
                return
            confirmation = await self.post_finalize(prepared)
            await interaction.followup.send(confirmation, ephemeral=True)

        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
            self.logger.exception(
                "finalize_applicant failed: invoker=%s target=%s channel=%s",
                interaction.user.id,
                applicant.id,
                getattr(channel or interaction.channel, "id", None),
            )
            await fail(interaction)

    @app_commands.command(
        name="recstatements",
        description="Send an Under 16 or Hyperactive message to an applicant."
    )
    @app_commands.describe(
        message="Recruitment message to send.",
        applicant="Applicant who should receive the message.",
        channel="Applicant ticket to post in. Leave empty to use this channel.",
        additional_notes="Extra note to add below the recruitment message."
    )
    @app_commands.choices(message=RARE_STATEMENT_CHOICES)
    async def recstatements(
        self,
        interaction: discord.Interaction,
        message: app_commands.Choice[str],
        applicant: discord.Member,
        channel: Optional[discord.TextChannel] = None,
        additional_notes: str = None
    ):
        """Send rarely used recruitment statements to applicants"""
        
        if not any(role.id in (CORE | RECRUITERS) for role in interaction.user.roles):
            await deny(interaction)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            target_channel = channel or interaction.channel
             
            prepared = self.prepare_recstatement(
                message.value, applicant, target_channel, additional_notes,
            )
            if prepared["issue"]:
                if not isinstance(target_channel, discord.TextChannel):
                    await interaction.followup.send(prepared["issue"], ephemeral=True)
                else:
                    await warn(interaction, prepared["issue"])
                return
            confirmation_msg = await self.post_recstatement(prepared)
            await interaction.followup.send(confirmation_msg, ephemeral=True)
            
        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
            self.logger.exception(
                "recstatements failed: invoker=%s template=%s target=%s channel=%s",
                interaction.user.id,
                message.value,
                applicant.id,
                getattr(channel or interaction.channel, "id", None),
            )
            await fail(interaction)

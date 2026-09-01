"""CWL signup reminders, roster announcement, and briefs."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone as dt_timezone
from typing import Any
from typing import List
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks
from elbow_helper.discord.interactions import deny
from elbow_helper.discord.interactions import warn
from elbow_helper.discord.timezones import build_timezone_choices

from elbow_helper.configuration.channels import CLAN_CWL_INFO_CHANNELS
from elbow_helper.configuration.channels import CLAN_TRANSFERS
from elbow_helper.configuration.channels import CWL_SIGNUP
from elbow_helper.configuration.clans import CLAN_CWL_ROLE_IDS
from elbow_helper.configuration.roles import CWL_HELPERS
from elbow_helper.configuration.roles import LEAD
from elbow_helper.configuration.roles import LEAD_PLUS
from elbow_helper.infrastructure.time import resolve_timezone
from .helpers import wait_for_boot_complete
from .config import BRIEF_MODES
from .config import BONUS_AUTOMATION_START_MONTH_KEY
from .config import CLAN_CHOICES
from .config import CWL_BONUS_ECONOMY_ENABLED
from .config import CWL_HQ_CHANNEL_ID
from .config import ROSTER_DEADLINE_MODES
from .config import SCHEDULER_STATE_FILE
from .config import WAR_SPECIALIST_ROLE_MENTION
from .templates import BRIEF_TEMPLATES
from .templates import FIRST_REMINDER
from .templates import ROSTER_DELAYED_DEADLINE_SECTION
from .templates import ROSTER_SINGLE_DEADLINE_SECTION
from .templates import ROSTER_TEMPLATE
from .templates import SECOND_REMINDER_TEMPLATE
from .templates import SIGNUP_STATEMENT
from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic


LOGGER = logging.getLogger(__name__)
timezone = dt_timezone


def _signup_reminder_times(
    opens_at: datetime,
    closes_at: datetime,
) -> tuple[datetime | None, datetime | None]:
    final = closes_at - timedelta(days=2)
    if final <= opens_at:
        return None, None
    first = opens_at + timedelta(days=7)
    return (first if first < final else None), final


class CwlAnnouncementMixin:
    def _load_scheduler_state(self) -> dict[str, Any]:
        try:
            if os.path.exists(SCHEDULER_STATE_FILE):
                data = read_json(SCHEDULER_STATE_FILE)
                if isinstance(data, dict):
                    return data
        except (OSError, json.JSONDecodeError, TypeError) as e:
            LOGGER.exception("Failed to load scheduler state: %s", e)
        return {}

    def _load_sent_keys(self) -> List[str]:
        data = self._load_scheduler_state()
        sent_keys = data.get("sent_keys", [])
        if not isinstance(sent_keys, list):
            return []
        return [key for key in sent_keys if isinstance(key, str)]

    def _save_scheduler_state(self):
        try:
            # Persist scheduler state so reminder sends and follow-up cleanup survive restarts.
            write_json_atomic(
                SCHEDULER_STATE_FILE,
                {"sent_keys": sorted(self._sent_keys)},
                indent=2,
            )
        except (OSError, TypeError) as e:
            LOGGER.exception("Failed to save scheduler state: %s", e)

    async def tz_autocomplete(self, interaction: discord.Interaction, current: str):
        return build_timezone_choices(current)

    def _parse_dd_hh_mm(self, value: str) -> tuple[Optional[int], Optional[int], Optional[int]]:
        try:
            day_part, time_part = value.split("-")
            hour_part, minute_part = time_part.split(":")
            return int(day_part), int(hour_part), int(minute_part)
        except (AttributeError, TypeError, ValueError):
            return None, None, None

    def _resolve_next_local_deadline(
        self,
        now_local: datetime,
        day: int,
        hour: int,
        minute: int,
    ) -> datetime:
        year = now_local.year
        month = now_local.month
        for _ in range(24):
            try:
                candidate = datetime(year, month, day, hour, minute, tzinfo=now_local.tzinfo)
            except ValueError:
                if month == 12:
                    year += 1
                    month = 1
                else:
                    month += 1
                continue
            if candidate > now_local:
                return candidate
            if month == 12:
                year += 1
                month = 1
            else:
                month += 1
        raise ValueError("Could not resolve a future deadline from DD-HH:mm")

    def _build_roster_deadline_section(
        self,
        deadline_mode: str,
        deadline_text: str,
        delayed_deadline_text: Optional[str] = None,
    ) -> str:
        if deadline_mode == "single":
            return ROSTER_SINGLE_DEADLINE_SECTION.format(deadline=deadline_text)
        if delayed_deadline_text is None:
            raise ValueError("Delayed deadline text is required for preferred_delayed mode.")
        return ROSTER_DELAYED_DEADLINE_SECTION.format(
            deadline=deadline_text,
            delayed_deadline=delayed_deadline_text,
        )

    async def _send_chunked_ephemeral_preview(
        self,
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        for chunk in self._chunk_content(content):
            await interaction.followup.send(chunk, ephemeral=True)

    @tasks.loop(minutes=1)
    async def reminder_loop(self):
        await self.bot.wait_until_ready()
        try:
            # CWL signup timing follows the configured roster's live timing or schedule.
            # The loop runs every minute and fires each action once per roster cycle.
            now = datetime.now(dt_timezone.utc)
            await self._cleanup_expired_transfer_reminders(now)
            year = now.year
            month = now.month
            automation = await self.roster_automation.cwl_signup_window(now)

            async def resolve_channel(channel_id: int) -> Optional[discord.abc.Messageable]:
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        return None
                return channel

            def should_fire(key: str, target: datetime, max_late: timedelta = timedelta(hours=24)) -> bool:
                # Allow late catch-up after downtime while preventing stale sends days later.
                if key in self._sent_keys:
                    return False
                if now < target:
                    return False
                return (now - target) <= max_late

            automation_active = False
            if automation is not None:
                roster, signup_window = automation
                signup_time_utc = signup_window.opens_at
                suffix = signup_window.cycle_key
                signup_channel_id = CWL_SIGNUP
                automation_active = signup_time_utc <= now < signup_window.closes_at

                async def send_signup_event(
                    event_key: str,
                    sent_key: str,
                    target: datetime,
                    content: str,
                    *,
                    react: bool = False,
                ) -> None:
                    if now < target or (now - target) > timedelta(hours=24):
                        return
                    if sent_key in self._sent_keys:
                        await self.roster_automation.claim_event(
                            roster.id,
                            signup_window.cycle_key,
                            event_key,
                        )
                        return
                    channel = await resolve_channel(signup_channel_id)
                    if channel is None:
                        return
                    claimed = await self.roster_automation.claim_event(
                        roster.id,
                        signup_window.cycle_key,
                        event_key,
                    )
                    if not claimed:
                        self._sent_keys.add(sent_key)
                        self._save_scheduler_state()
                        return
                    try:
                        message = await channel.send(content)
                    except Exception:
                        await self.roster_automation.release_event(
                            roster.id,
                            signup_window.cycle_key,
                            event_key,
                        )
                        raise
                    self._sent_keys.add(sent_key)
                    self._save_scheduler_state()
                    if react:
                        await self._react_with_detected_emojis(message, content)

                signup_key = f"signup-{suffix}"
                await send_signup_event(
                    "opening",
                    signup_key,
                    signup_time_utc,
                    SIGNUP_STATEMENT,
                    react=True,
                )

                first_reminder_time, final_reminder_time = _signup_reminder_times(
                    signup_time_utc,
                    signup_window.closes_at,
                )
                first_key = f"reminder1-{suffix}"
                if first_reminder_time is not None:
                    await send_signup_event(
                        "first_reminder",
                        first_key,
                        first_reminder_time,
                        FIRST_REMINDER,
                    )
                final_key = f"reminder2-{suffix}"
                if final_reminder_time is not None:
                    await send_signup_event(
                        "final_reminder",
                        final_key,
                        final_reminder_time,
                        SECOND_REMINDER_TEMPLATE,
                    )

            # Cleanup CWL reminders on the first day of the month at 08:00 UTC.
            cleanup_time = datetime(year, month, 1, 8, 0, tzinfo=dt_timezone.utc)
            cleanup_key = f"cleanup-{year}-{month}"
            if (
                now >= cleanup_time
                and cleanup_key not in self._sent_keys
                and not automation_active
            ):
                await self._cleanup_reminder_channel()
                self._sent_keys.add(cleanup_key)
                self._save_scheduler_state()

            bonus_month_key = (year * 12) + month
            bonus_board_time = datetime(year, month, 13, 8, 0, tzinfo=dt_timezone.utc)
            bonus_board_key = f"bonus-board-{year}-{month}"
            if (
                CWL_BONUS_ECONOMY_ENABLED
                and bonus_month_key >= BONUS_AUTOMATION_START_MONTH_KEY
                and should_fire(bonus_board_key, bonus_board_time, max_late=timedelta(days=31))
            ):
                channel = await resolve_channel(CWL_HQ_CHANNEL_ID)
                if isinstance(channel, discord.TextChannel):
                    await self._post_bonus_dashboard(
                        mode="final",
                        target_channel=channel,
                        ping_helper=True,
                    )
                    self._sent_keys.add(bonus_board_key)
                    self._save_scheduler_state()
        except Exception as e:
            LOGGER.exception("reminder loop iteration failed: %s", e)


    @reminder_loop.before_loop
    async def before_reminder_loop(self):
        await wait_for_boot_complete(self.bot)


    async def _cleanup_reminder_channel(self) -> None:
        # Delete prior CWL reminder posts so the channel resets each month.
        channel = self.bot.get_channel(CWL_SIGNUP)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(CWL_SIGNUP)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        roster_message_ids = await self.roster_queries.post_message_ids_for_channel(
            CWL_SIGNUP
        )
        try:
            async for msg in channel.history(limit=200, oldest_first=False):
                if (
                    msg.author
                    and msg.author.id == self.bot.user.id
                    and msg.id not in roster_message_ids
                ):
                    try:
                        await msg.delete()
                    except (discord.Forbidden, discord.HTTPException):
                        continue
        except (discord.Forbidden, discord.HTTPException):
            return


    @app_commands.choices(deadline_mode=ROSTER_DEADLINE_MODES)
    @app_commands.describe(
        deadline_mode="Whether everyone shares one deadline or some clans receive extra time.",
        deadline="Main deadline in DD-HH:mm. Example: 01-20:00 means the 1st at 20:00.",
        timezone="Timezone to use for the deadline.",
        delayed_deadline="Later deadline in DD-HH:mm. Example: 02-20:00 means the 2nd at 20:00.",
        intro="Custom opening line for the roster announcement.",
        preview="Show the finished announcement without posting it.",
    )
    @app_commands.autocomplete(timezone=tz_autocomplete)
    async def roster_announcement(
        self,
        interaction: discord.Interaction,
        deadline_mode: app_commands.Choice[str],
        deadline: str,
        timezone: str,
        delayed_deadline: Optional[str] = None,
        intro: Optional[str] = None,
        preview: bool = False,
    ):
        if not self._has_any_role(interaction, set(LEAD)):
            await deny(interaction)
            return
        status_text = "Building the roster announcement preview..." if preview else "Posting roster announcement..."
        await interaction.response.send_message(status_text, ephemeral=True)
        mode_value = deadline_mode.value
        LOGGER.info(
            "roster_announcement by %s mode=%s deadline=%s delayed=%s tz=%s preview=%s",
            interaction.user,
            mode_value,
            deadline,
            delayed_deadline,
            timezone,
            preview,
        )
        target_channel = None
        if not preview:
            target_channel = interaction.client.get_channel(CLAN_TRANSFERS)
            if not target_channel:
                try:
                    target_channel = await interaction.client.fetch_channel(CLAN_TRANSFERS)
                    LOGGER.info("fetched roster channel %s", target_channel)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as fetch_err:
                    LOGGER.warning("roster channel fetch failed: %s", fetch_err)
                    await interaction.followup.send("The CWL roster channel hasn't been set up. Check the CWL setup.", ephemeral=True)
                    return
        now = datetime.now(dt_timezone.utc)
        deadline_day, deadline_hour, deadline_minute = self._parse_dd_hh_mm(deadline)
        if deadline_day is None:
            await warn(
                interaction,
                "Enter the deadline as DD-HH:mm: the day of the month followed by 24-hour time. For example, 01-20:00 means the 1st at 20:00.",
            )
            return

        delayed_day = delayed_hour = delayed_minute = None
        if mode_value == "preferred_delayed":
            if not delayed_deadline:
                await interaction.followup.send(
                    "Enter the later deadline when some clans receive extra time.",
                    ephemeral=True,
                )
                return
            delayed_day, delayed_hour, delayed_minute = self._parse_dd_hh_mm(delayed_deadline)
            if delayed_day is None:
                await warn(
                    interaction,
                    "Enter the later deadline as DD-HH:mm: the day of the month followed by 24-hour time. For example, 02-20:00 means the 2nd at 20:00.",
                )
                return
        try:
            tz_info = resolve_timezone(timezone)
            if tz_info is None:
                await warn(
                    interaction,
                    "Choose a timezone from the list.",
                )
                return
            now_local = now.astimezone(tz_info)
            deadline_local = self._resolve_next_local_deadline(
                now_local,
                deadline_day,
                deadline_hour,
                deadline_minute,
            )
            delayed_local = None
            if mode_value == "preferred_delayed":
                delayed_local = self._resolve_next_local_deadline(
                    now_local,
                    delayed_day,
                    delayed_hour,
                    delayed_minute,
                )
                if delayed_local < deadline_local:
                    await interaction.followup.send(
                        "Delayed deadline must be the same as or after the main deadline.",
                        ephemeral=True,
                    )
                    return
            deadline_ts = int(deadline_local.timestamp())
            delayed_ts = int(delayed_local.timestamp()) if delayed_local else None
            LOGGER.info(
                "resolved tz=%s mode=%s deadline_local=%s delayed_local=%s",
                tz_info,
                mode_value,
                deadline_local,
                delayed_local,
            )
        except (TypeError, ValueError, OverflowError) as e:
            LOGGER.warning("error computing timestamps: %s", e)
            await warn(
                interaction,
                "Enter the time as `DD-HH:mm` and choose a timezone from the list.",
            )
            return

        deadline_text = f"<t:{deadline_ts}:F> (<t:{deadline_ts}:R>)"
        delayed_deadline_text = (
            f"<t:{delayed_ts}:F> (<t:{delayed_ts}:R>)" if delayed_ts is not None else None
        )

        hub_message_url = self._transfer_hub_url()
        release_cycles = None
        if not preview:
            if not await self.ensure_transfer_hub():
                await interaction.followup.send(
                    "I couldn't update **CWL Rosters and Transfers**, so the announcement "
                    "wasn't posted.",
                    ephemeral=True,
                )
                return
            hub_message_url = self._transfer_hub_url()
            release_cycles = await self._current_cwl_roster_cycles(target_channel.guild.id)
            if release_cycles is None:
                await interaction.followup.send(
                    "CWL rosters aren't available, so the announcement wasn't posted.",
                    ephemeral=True,
                )
                return
        if hub_message_url is None:
            await interaction.followup.send(
                "The **CWL Rosters and Transfers** message isn't available.",
                ephemeral=True,
            )
            return

        intro_text = intro.strip() if intro else (
            "You may already have been pinged or invited in-game. If not, check where "
            "you’re playing now and whether you still need to move."
        )
        deadline_section = self._build_roster_deadline_section(
            mode_value,
            deadline_text,
            delayed_deadline_text,
        )
        content = ROSTER_TEMPLATE.format(
            intro_text=intro_text,
            deadline_section=deadline_section,
            hub_message_url=hub_message_url,
            war_specialist_role=WAR_SPECIALIST_ROLE_MENTION,
        )
        if preview:
            await self._send_chunked_ephemeral_preview(interaction, content)
            await interaction.followup.send("Preview only. Nothing was posted.", ephemeral=True)
            return
        try:
            LOGGER.info("sending roster announcement to channel %s", target_channel.id)
            sent_messages = await self._send_chunked(target_channel, content)
            if sent_messages:
                last_message = sent_messages[-1]
                await self._react_with_detected_emojis(last_message, content)
            self._release_cwl_placements(release_cycles)
            if await self.ensure_transfer_hub():
                await interaction.followup.send(
                    "The roster announcement is live.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "The roster announcement is live, but **Where Am I Playing?** and "
                    "**CWL Channels** couldn't be enabled.",
                    ephemeral=True,
                )
        except (discord.Forbidden, discord.HTTPException) as send_err:
            LOGGER.exception("failed to send roster announcement: %s", send_err)
            await interaction.followup.send(
                "I couldn't post the roster announcement. Try again in a moment.",
                ephemeral=True,
            )


    @app_commands.choices(clan=CLAN_CHOICES, mode=BRIEF_MODES)
    @app_commands.describe(
        clan="Clan whose CWL info channel should get the brief.",
        mode="Version of the CWL brief to post.",
        helper_cwl="CWL helper to mention in the brief.",
        rotations="Whether to include the daily rotations section.",
        lead_cwl="Needed for Highly Motivated and Mainline Pushing; leave blank for other modes.",
        intro="Custom opening line for the CWL brief.",
    )
    async def cwl_brief(
        self,
        interaction: discord.Interaction,
        clan: app_commands.Choice[str],
        mode: app_commands.Choice[str],
        helper_cwl: str,
        rotations: bool,
        lead_cwl: Optional[str] = None,
        intro: Optional[str] = None,
    ):
        if not self._has_any_role(interaction, (LEAD_PLUS | CWL_HELPERS)):
            await deny(interaction)
            return
        await interaction.response.send_message("Posting CWL brief...", ephemeral=True)
        clan_code = clan.value
        channel_id = CLAN_CWL_INFO_CHANNELS.get(clan_code)
        team_role_id = CLAN_CWL_ROLE_IDS.get(clan_code)
        if not channel_id or not team_role_id:
            await interaction.followup.send("CWL channels haven't been set up for that clan. Check the CWL setup.", ephemeral=True)
            return
        target_channel = interaction.client.get_channel(channel_id)
        if not target_channel:
            try:
                target_channel = await interaction.client.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                LOGGER.warning("brief fetch channel failed: %s", e)
                await interaction.followup.send(
                    "The clan's CWL info channel hasn't been set up. Check that clan's CWL setup.",
                    ephemeral=True,
                )
                return

        template = BRIEF_TEMPLATES.get(mode.value)
        if not template:
            await interaction.followup.send(
                "That CWL brief version isn't available. Choose another version.",
                ephemeral=True,
            )
            return
        template = self._apply_brief_overrides(template, intro, rotations)
        cwl_team_role = f"<@&{team_role_id}>"
        lead_str = lead_cwl or "TBD"
        content = template.format(
            cwl_team_role=cwl_team_role,
            clan_post_channel=target_channel.mention,
            lead_cwl=lead_str,
            helper_cwl=helper_cwl,
        )
        try:
            await self._send_chunked(target_channel, content)
            await interaction.followup.send(f"Brief posted to {target_channel.mention}.", ephemeral=True)
        except (discord.Forbidden, discord.HTTPException, RuntimeError) as e:
            LOGGER.exception("failed to send brief: %s", e)
            await interaction.followup.send(
                "I couldn't post the brief. Try again in a moment.",
                ephemeral=True,
            )


    def _apply_brief_overrides(
        self, template: str, intro: Optional[str], include_rotations: bool
    ) -> str:
        # Override the first intro line and optionally include/remove rotations.
        lines = template.splitlines()
        if intro:
            for idx in range(1, len(lines)):
                if lines[idx].strip():
                    lines[idx] = intro
                    break

        def is_rotations_marker(line: str) -> bool:
            cleaned = line.strip().lower().replace(" ", "")
            return cleaned in {"#rotations", "(#rotations)"}

        if not include_rotations:
            updated = []
            skipping = False
            for line in lines:
                if skipping:
                    if line.strip().startswith("#"):
                        skipping = False
                        updated.append(line)
                    else:
                        continue
                elif is_rotations_marker(line):
                    skipping = True
                else:
                    updated.append(line)
            lines = updated

        return "\n".join(lines).strip()

"""Examiner panel, roster, and matching logic."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

import discord
from elbow_helper.discord.pagination import ADAPTIVE_JUMP_THRESHOLD
from elbow_helper.discord.pagination import FIRST_PAGE_LABEL
from elbow_helper.discord.pagination import format_page_footer
from elbow_helper.discord.pagination import LAST_PAGE_LABEL
from elbow_helper.discord.pagination import NEXT_PAGE_LABEL
from elbow_helper.discord.pagination import PREV_PAGE_LABEL
from elbow_helper.discord.views import BaseErrorModal
from elbow_helper.discord.views import BaseTimeoutView

from elbow_helper.configuration.channels import EXAMINATION_PANEL_THREAD
from elbow_helper.configuration.clans import CLAN_LEADERSHIP_ROLE_IDS
from elbow_helper.configuration.clans import CLAN_MEMBER_ROLE_IDS
from elbow_helper.configuration.roles import EXAMINERS, LEAD
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX, DEFAULT_THUMBNAIL_URL
from elbow_helper.domain.timezones import format_timezone_display

from .availability import _canonicalize_availability_text
from .availability import _format_availability_display
from .availability import parse_availability_windows
from .availability import parse_timezone_offset
from .availability import availability_matches
from .availability import availability_matches_structured
from .config import ROSTER_PAGE_SIZE
from .config import TIMEZONE_SELECT_OPTIONS
from .config import TH_COVERAGE_OPTIONS

if TYPE_CHECKING:
    from .cog import Examination



class ExaminationPanelMixin:
    async def _get_panel_thread(self) -> Optional[discord.Thread]:
        channel = self.bot.get_channel(EXAMINATION_PANEL_THREAD)
        if isinstance(channel, discord.Thread):
            return channel
        try:
            fetched = await self.bot.fetch_channel(EXAMINATION_PANEL_THREAD)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.Thread) else None
    def _collect_examiners(self, guild: discord.Guild) -> List[discord.Member]:
        # Only include examiners that registered on the panel.
        members: List[discord.Member] = []
        roster = self._get_examiner_roster()
        for user_id in roster.keys():
            try:
                member = guild.get_member(int(user_id))
            except (TypeError, ValueError):
                continue
            if not member:
                continue
            if any(role.id in EXAMINERS for role in member.roles):
                members.append(member)
        return list({member.id: member for member in members}.values())

    def _match_examiners(
        self,
        examiners: List[discord.Member],
        roster: Dict[str, Any],
        th_level: Optional[int],
        availability: str,
        *,
        applicant_windows: Optional[List[Dict[str, Any]]] = None,
        clan_codes: Optional[List[str]] = None,
        ignore_ids: Optional[Set[int]] = None,
    ) -> List[discord.Member]:
        # Filter by role settings, TH match, availability, and clan roles if needed.
        ignore_ids = ignore_ids or set()
        matches: List[discord.Member] = []
        for member in examiners:
            if member.id in ignore_ids:
                continue
            profile = roster.get(str(member.id))
            if not profile:
                continue
            status_raw = (profile.get("status") or "").strip()
            if status_raw and status_raw.lower() != "active":
                continue
            ths = profile.get("th_levels") or []
            if th_level is not None:
                if not ths or th_level not in ths:
                    continue
            if availability or applicant_windows:
                if not profile.get("availability"):
                    continue
                timezone_text = profile.get("timezone") or "UTC"
                if applicant_windows:
                    if not availability_matches_structured(
                        applicant_windows,
                        profile.get("availability", ""),
                        timezone_text,
                    ):
                        continue
                else:
                    if not availability_matches(availability, profile.get("availability", ""), timezone_text):
                        continue
            if clan_codes:
                member_roles = {role.id for role in member.roles}
                if not any(CLAN_MEMBER_ROLE_IDS.get(code) in member_roles for code in clan_codes):
                    continue
            matches.append(member)
        return matches

    def _select_clan_promo_examiners(
        self,
        examiners: List[discord.Member],
        roster: Dict[str, Any],
        th_level: Optional[int],
        availability: str,
        *,
        applicant_windows: Optional[List[Dict[str, Any]]] = None,
        ignore_ids: Optional[Set[int]] = None,
        min_count: int = 2,
    ) -> Tuple[List[discord.Member], bool]:
        # Pick exact TH matches, then expand to +1/+2 to reach the minimum.
        if th_level is None or not availability:
            return [], False
        ignore_ids = ignore_ids or set()
        selected: List[discord.Member] = []
        used_fallback = False
        exact = self._match_examiners(
            examiners,
            roster,
            th_level,
            availability,
            applicant_windows=applicant_windows,
            ignore_ids=ignore_ids,
        )
        if exact:
            selected.extend(exact)
        def _add_unique(source: List[discord.Member]) -> None:
            existing_ids = {member.id for member in selected}
            for member in source:
                if member.id not in existing_ids:
                    selected.append(member)
                    existing_ids.add(member.id)
        if len(selected) < min_count:
            plus_one = self._match_examiners(
                examiners,
                roster,
                min(th_level + 1, 20),
                availability,
                applicant_windows=applicant_windows,
                ignore_ids=ignore_ids,
            )
            if plus_one:
                _add_unique(plus_one)
                used_fallback = True
        if len(selected) < min_count:
            plus_two = self._match_examiners(
                examiners,
                roster,
                min(th_level + 2, 20),
                availability,
                applicant_windows=applicant_windows,
                ignore_ids=ignore_ids,
            )
            if plus_two:
                _add_unique(plus_two)
                used_fallback = True
        return selected, used_fallback

    async def _post_panel(self) -> None:
        # Update or create the examiner panel message.
        thread = await self._get_panel_thread()
        if not thread:
            return
        if thread.archived:
            try:
                await thread.edit(archived=False, reason="Update the examiner panel")
            except (discord.Forbidden, discord.HTTPException):
                self.logger.warning("Examiner panel unarchive skipped: thread_id=%s", thread.id)
                return
        entries = self._get_roster_entries()
        total = len(entries)
        active = sum(1 for entry in entries if entry.get("status") == "Active")
        away = sum(1 for entry in entries if entry.get("status") == "Away")
        complete = sum(1 for entry in entries if entry.get("profile_complete"))
        missing_th_coverage = sum(1 for entry in entries if not entry.get("has_th_levels"))
        missing_availability = sum(
            1 for entry in entries if not entry.get("availability_raw")
        )
        invalid_availability = sum(
            1
            for entry in entries
            if entry.get("availability_raw") and not entry.get("availability_valid")
        )
        latest_profile_update_ts: Optional[int] = None
        for entry in entries:
            updated_raw = entry.get("updated_at")
            if not updated_raw:
                continue
            try:
                updated_dt = datetime.fromisoformat(str(updated_raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            ts = int(updated_dt.timestamp())
            if latest_profile_update_ts is None or ts > latest_profile_update_ts:
                latest_profile_update_ts = ts
        embed = discord.Embed(
            title="Examiner Panel",
            description="Set your availability and status with the controls below.",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        examiner_count = "1 examiner" if total == 1 else f"{total} examiners"
        embed.add_field(
            name="Examiner Summary",
            value=(
                f"**{examiner_count}**\n"
                f"Active: **{active}** • Away: **{away}**"
            ),
            inline=False,
        )
        incomplete = total - complete
        if incomplete:
            issue_lines = [f"Profiles needing updates: **{incomplete}**/{total if total else 0}"]
            if missing_th_coverage:
                issue_lines.append(f"Town Hall coverage missing: **{missing_th_coverage}**")
            if missing_availability:
                issue_lines.append(f"Availability missing: **{missing_availability}**")
            if invalid_availability:
                issue_lines.append(
                    f"This availability couldn't be read: {invalid_availability}. "
                    "Use a format like `Daily 10:00-22:00` or `Mon-Fri 07:00-17:00`."
                )
            embed.add_field(
                name="Profiles Needing Updates",
                value="\n".join(issue_lines),
                inline=False,
            )
        msg_id = self.state.get("panel_message_id")
        if msg_id:
            try:
                msg = await thread.fetch_message(msg_id)
                await msg.edit(embed=embed, view=self.panel_view)
                return
            except discord.NotFound:
                self.logger.debug(
                    "Examiner panel message missing; posting replacement: thread_id=%s message_id=%s",
                    thread.id,
                    msg_id,
                )
            except discord.Forbidden:
                self.logger.warning(
                    "Missing permissions updating examiner panel: thread_id=%s message_id=%s",
                    thread.id,
                    msg_id,
                )
            except discord.HTTPException:
                self.logger.exception("Examiner panel message update failed: thread_id=%s", thread.id)
        msg = await thread.send(embed=embed, view=self.panel_view)
        self.state["panel_message_id"] = msg.id
        self._save()

    def _ensure_examiner_profile(self, member: discord.Member) -> Dict[str, Any]:
        # Create a default profile entry for the examiner.
        roster = self._get_examiner_roster()
        profile = roster.get(str(member.id))
        if not profile:
            profile = {
                "name": member.display_name,
                "th_levels": [],
                "availability": "",
                "status": "Active",
                "timezone": "UTC",
                "updated_at": None,
                "availability_valid": False,
                "profile_complete": False,
            }
            roster[str(member.id)] = profile
        else:
            profile["name"] = member.display_name
        self._refresh_examiner_profile(profile)
        return profile

    @staticmethod
    def _normalize_examiner_status(status_raw: Optional[str]) -> str:
        value = (status_raw or "").strip().lower()
        if value == "away":
            return "Away"
        return "Active"

    def _refresh_examiner_profile(self, profile: Dict[str, Any], *, touched: bool = False) -> None:
        # Keep profile metadata consistent for panel analytics and routing quality.
        profile["name"] = (profile.get("name") or "Unknown").strip() or "Unknown"
        profile["th_levels"] = sorted({int(v) for v in (profile.get("th_levels") or []) if str(v).isdigit()})
        profile["status"] = self._normalize_examiner_status(profile.get("status"))
        timezone_text = (profile.get("timezone") or "UTC").strip() or "UTC"
        profile["timezone"] = timezone_text
        availability_raw = (profile.get("availability") or "").strip()
        canonical = (
            _canonicalize_availability_text(
                availability_raw,
                timezone_text,
                allow_input_timezone=False,
            )
            if availability_raw
            else None
        )
        if canonical:
            availability_raw = canonical
        profile["availability"] = availability_raw
        windows = parse_availability_windows(availability_raw) if availability_raw else []
        raw_tz = parse_timezone_offset(availability_raw) if availability_raw else None
        profile_tz = parse_timezone_offset(timezone_text)
        profile["availability_valid"] = bool(windows) and (raw_tz is not None or profile_tz is not None)
        profile["profile_complete"] = bool(
            profile["th_levels"] and profile["availability_valid"] and timezone_text
        )
        if touched:
            profile["updated_at"] = datetime.now(timezone.utc).isoformat()

    def _has_panel_permissions(self, member: discord.Member) -> bool:
        # Allow examiners and leadership to update the panel.
        if any(role.id in EXAMINERS for role in member.roles):
            return True
        if any(role.id in LEAD for role in member.roles):
            return True
        if any(role.id in CLAN_LEADERSHIP_ROLE_IDS.values() for role in member.roles):
            return True
        return False

    def _get_roster_entries(self) -> List[Dict[str, Any]]:
        # Build a sorted roster list for pagination.
        roster = self._get_examiner_roster()
        entries: List[Dict[str, Any]] = []
        for user_id, profile in roster.items():
            self._refresh_examiner_profile(profile)
            name = profile.get("name") or f"<@{user_id}>"
            ths = ", ".join(map(str, profile.get("th_levels") or [])) or "None"
            raw_availability = profile.get("availability") or ""
            availability = _format_availability_display(raw_availability)
            if not availability:
                availability = "Not set"
            status = self._normalize_examiner_status(profile.get("status"))
            entries.append(
                {
                    "id": user_id,
                    "name": name,
                    "ths": ths,
                    "availability": availability,
                    "status": status,
                    "timezone": profile.get("timezone") or "UTC",
                    "availability_raw": raw_availability,
                    "has_th_levels": bool(profile.get("th_levels")),
                    "availability_valid": bool(profile.get("availability_valid")),
                    "profile_complete": bool(profile.get("profile_complete")),
                    "updated_at": profile.get("updated_at"),
                }
            )
        status_rank = {"Active": 0, "Away": 1}
        entries.sort(key=lambda item: (status_rank.get(item["status"], 9), item["name"].lower()))
        return entries

    def _build_roster_page(self, entries: List[Dict[str, Any]], index: int) -> discord.Embed:
        # Build a paginated roster page with multiple examiners.
        if not entries:
            embed = discord.Embed(
                title="Examiner Roster",
                description="No examiner profiles yet.",
                color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
            return embed
        total_pages = (len(entries) + ROSTER_PAGE_SIZE - 1) // ROSTER_PAGE_SIZE
        safe_index = max(0, min(index, total_pages - 1))
        start = safe_index * ROSTER_PAGE_SIZE
        page_entries = entries[start : start + ROSTER_PAGE_SIZE]
        embed = discord.Embed(
            title="Examiner Roster",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        for entry in page_entries:
            availability_text = entry.get("availability") or "Not set"
            if availability_text != "Not set":
                windows = [part.strip() for part in availability_text.split("|") if part.strip()]
                if windows:
                    availability_text = windows[0]
                    if len(windows) > 1:
                        availability_text = f"{availability_text} (+{len(windows) - 1} more)"
            if len(availability_text) > 64:
                availability_text = f"{availability_text[:61]}..."
            embed.add_field(
                name=entry["name"],
                value=(
                    f"Town Halls: {entry['ths']} • "
                    f"Timezone: {format_timezone_display(str(entry['timezone']))} • "
                    f"Status: {entry['status']}\n"
                    f"Availability: {availability_text}"
                ),
                inline=False,
            )
        embed.set_footer(text=format_page_footer(safe_index + 1, total_pages))
        return embed
    def _has_exam_permissions(self, member: discord.Member) -> bool:
        return self._has_panel_permissions(member)


class ExaminerAvailabilityModal(BaseErrorModal):
    def __init__(self, cog: Examination):
        super().__init__(title="Set Examiner Availability")
        self.cog = cog
        self.availability = discord.ui.TextInput(
            label="Availability",
            placeholder="Examples: Daily 10:00-22:00 | Mon-Fri 07:00-17:00",
            required=True,
            max_length=120,
        )
        self.add_item(self.availability)

    async def on_submit(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "I couldn't load your member details. Please try again.",
                ephemeral=True,
            )
            return
        if not self.cog._has_panel_permissions(interaction.user):
            await interaction.response.send_message("You don't have permission to edit this.", ephemeral=True)
            return
        profile = self.cog._ensure_examiner_profile(interaction.user)
        profile["name"] = interaction.user.display_name
        raw_input = self.availability.value.strip()
        timezone_text = profile.get("timezone") or "UTC"
        canonical = _canonicalize_availability_text(
            raw_input,
            timezone_text,
            allow_input_timezone=False,
        )
        if not canonical:
            await interaction.response.send_message(
                "Enter availability like "
                "`Daily 10:00-22:00`, `Mon-Fri 07:00-17:00`, or `Sat-Sun 12:00-18:00`.",
                ephemeral=True,
            )
            return
        profile["availability"] = canonical
        self.cog._refresh_examiner_profile(profile, touched=True)
        self.cog._save()
        await self.cog._post_panel()
        await interaction.response.send_message(
            "Your availability has been saved.\n"
            f"Timezone: {format_timezone_display(str(timezone_text))}\n"
            f"Availability: `{canonical}`",
            ephemeral=True,
        )


class ExaminerPanelView(BaseTimeoutView):
    def __init__(self, cog: Examination):
        super().__init__(timeout=None)
        self.cog = cog

    def _check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        return self.cog._has_panel_permissions(interaction.user)

    @discord.ui.select(
        placeholder="Set Town Hall coverage",
        custom_id="exam_panel_th_select",
        min_values=1,
        max_values=8,
        row=0,
        options=TH_COVERAGE_OPTIONS,
    )
    async def th_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not self._check(interaction):
            await interaction.response.send_message("You don't have permission to edit this.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "I couldn't load your member details. Please try again.",
                ephemeral=True,
            )
            return
        levels = sorted({int(value) for value in select.values})
        profile = self.cog._ensure_examiner_profile(interaction.user)
        profile["name"] = interaction.user.display_name
        profile["th_levels"] = levels
        self.cog._refresh_examiner_profile(profile, touched=True)
        self.cog._save()
        await self.cog._post_panel()
        await interaction.response.send_message("Your Town Hall coverage has been updated.", ephemeral=True)

    @discord.ui.select(
        placeholder="Set status",
        custom_id="exam_panel_status_select",
        min_values=1,
        max_values=1,
        row=1,
        options=[
            discord.SelectOption(label="Active", value="Active"),
            discord.SelectOption(label="Away", value="Away"),
        ],
    )
    async def status_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not self._check(interaction):
            await interaction.response.send_message("You don't have permission to edit this.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "I couldn't load your member details. Please try again.",
                ephemeral=True,
            )
            return
        profile = self.cog._ensure_examiner_profile(interaction.user)
        profile["name"] = interaction.user.display_name
        profile["status"] = select.values[0]
        self.cog._refresh_examiner_profile(profile, touched=True)
        self.cog._save()
        await self.cog._post_panel()
        await interaction.response.send_message("Your examiner status has been updated.", ephemeral=True)

    @discord.ui.select(
        placeholder="Set timezone",
        custom_id="exam_panel_timezone_select",
        min_values=1,
        max_values=1,
        row=2,
        options=TIMEZONE_SELECT_OPTIONS,
    )
    async def timezone_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not self._check(interaction):
            await interaction.response.send_message("You don't have permission to edit this.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "I couldn't load your member details. Please try again.",
                ephemeral=True,
            )
            return
        profile = self.cog._ensure_examiner_profile(interaction.user)
        profile["name"] = interaction.user.display_name
        profile["timezone"] = select.values[0]
        self.cog._refresh_examiner_profile(profile, touched=True)
        self.cog._save()
        await self.cog._post_panel()
        await interaction.response.send_message("Your timezone has been updated.", ephemeral=True)

    @discord.ui.button(
        label="Set Availability",
        style=discord.ButtonStyle.secondary,
        custom_id="exam_panel_set_availability",
        row=3,
    )
    async def set_availability(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check(interaction):
            await interaction.response.send_message("You don't have permission to edit this.", ephemeral=True)
            return
        await interaction.response.send_modal(ExaminerAvailabilityModal(self.cog))

    @discord.ui.button(
        label="My Profile",
        style=discord.ButtonStyle.secondary,
        custom_id="exam_panel_view_profile",
        row=3,
    )
    async def view_profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check(interaction):
            await interaction.response.send_message("You don't have permission to view this.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "I couldn't load your member details. Please try again.",
                ephemeral=True,
            )
            return
        profile = self.cog._ensure_examiner_profile(interaction.user)
        levels = ", ".join(map(str, profile.get("th_levels") or [])) or "Not set"
        availability = profile.get("availability") or "Not set"
        status = self.cog._normalize_examiner_status(profile.get("status"))
        timezone_text = profile.get("timezone") or "UTC"
        embed = discord.Embed(
            title="Your Examiner Profile",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.add_field(name="Town Hall Coverage", value=levels, inline=False)
        embed.add_field(name="Timezone", value=format_timezone_display(str(timezone_text)), inline=False)
        embed.add_field(name="Availability", value=_format_availability_display(availability), inline=False)
        embed.add_field(name="Status", value=status, inline=False)
        missing_fields: list[str] = []
        if not profile.get("th_levels"):
            missing_fields.append("TH coverage")
        if not timezone_text:
            missing_fields.append("timezone")
        if not profile.get("availability_valid"):
            missing_fields.append("availability")
        readiness_lines = [
            f"Availability: {'Valid' if profile.get('availability_valid') else 'Needs update'}",
            f"Profile: {'Complete' if profile.get('profile_complete') else 'Incomplete'}",
        ]
        if missing_fields:
            readiness_lines.append("")
            readiness_lines.append(f"Missing: {', '.join(missing_fields)}.")
        embed.add_field(
            name="Profile Status",
            value="\n".join(readiness_lines),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="View Roster",
        style=discord.ButtonStyle.secondary,
        custom_id="exam_panel_roster",
        row=3,
    )
    async def roster(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check(interaction):
            try:
                await interaction.response.send_message("You don't have permission to view this.", ephemeral=True)
            except discord.NotFound:
                return
            return
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            # Interaction expired before we could acknowledge it.
            return
        entries = self.cog._get_roster_entries()
        embed = self.cog._build_roster_page(entries, 0)
        total_pages = (len(entries) + ROSTER_PAGE_SIZE - 1) // ROSTER_PAGE_SIZE
        view = ExaminerRosterView(self.cog, entries, 0) if total_pages > 1 else None
        try:
            if view is None:
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            message = await interaction.followup.send(embed=embed, view=view, ephemeral=True, wait=True)
            view.bind_message(message)
        except discord.NotFound:
            # Expired interaction token / stale button click.
            return

    @discord.ui.button(
        label="Leave Roster",
        style=discord.ButtonStyle.secondary,
        custom_id="exam_panel_remove_self",
        row=4,
    )
    async def remove_self(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check(interaction):
            await interaction.response.send_message("You don't have permission to edit this.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "I couldn't load your member details. Please try again.",
                ephemeral=True,
            )
            return
        roster = self.cog._get_examiner_roster()
        if roster.pop(str(interaction.user.id), None) is not None:
            self.cog._save()
            await self.cog._post_panel()
            await interaction.response.send_message("You have been removed from the examiner roster.", ephemeral=True)
            return
        await interaction.response.send_message("You aren't on the examiner roster.", ephemeral=True)


class ExaminerRosterView(BaseTimeoutView):
    def __init__(self, cog: Examination, entries: List[Dict[str, Any]], index: int = 0):
        super().__init__(timeout=180)
        self.cog = cog
        self.entries = entries
        self.page_index = index
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        total_pages = max(1, (len(self.entries) + ROSTER_PAGE_SIZE - 1) // ROSTER_PAGE_SIZE)
        jump_visible = total_pages > ADAPTIVE_JUMP_THRESHOLD
        for button in (self.first, self.last):
            if jump_visible and button not in self.children:
                self.add_item(button)
            elif not jump_visible and button in self.children:
                self.remove_item(button)
        if hasattr(self, "first"):
            self.first.disabled = self.page_index <= 0
        if hasattr(self, "prev"):
            self.prev.disabled = self.page_index <= 0
        if hasattr(self, "next"):
            self.next.disabled = self.page_index >= total_pages - 1
        if hasattr(self, "last"):
            self.last.disabled = self.page_index >= total_pages - 1

    async def _update(self, interaction: discord.Interaction) -> None:
        embed = self.cog._build_roster_page(self.entries, self.page_index)
        self._sync_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label=FIRST_PAGE_LABEL, style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button):
        total_pages = max(1, (len(self.entries) + ROSTER_PAGE_SIZE - 1) // ROSTER_PAGE_SIZE)
        if total_pages <= ADAPTIVE_JUMP_THRESHOLD:
            await self._update(interaction)
            return
        self.page_index = 0
        await self._update(interaction)

    @discord.ui.button(label=PREV_PAGE_LABEL, style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page_index > 0:
            self.page_index -= 1
        await self._update(interaction)

    @discord.ui.button(label=NEXT_PAGE_LABEL, style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        total_pages = max(1, (len(self.entries) + ROSTER_PAGE_SIZE - 1) // ROSTER_PAGE_SIZE)
        if self.page_index < total_pages - 1:
            self.page_index += 1
        await self._update(interaction)

    @discord.ui.button(label=LAST_PAGE_LABEL, style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button):
        total_pages = max(1, (len(self.entries) + ROSTER_PAGE_SIZE - 1) // ROSTER_PAGE_SIZE)
        if total_pages <= ADAPTIVE_JUMP_THRESHOLD:
            await self._update(interaction)
            return
        self.page_index = total_pages - 1
        await self._update(interaction)

"""Clan-promotion intake views and helpers."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import TYPE_CHECKING
from typing import Any
from typing import Dict
from typing import Optional

import discord
from elbow_helper.discord.views import BaseTimeoutView

from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL

from .logic import PROMO_SOURCES
from .logic import apply_completed_route
from .logic import default_case_fields
from .logic import is_valid_route
from .logic import requires_exam
from .logic import route_summary
from .logic import valid_targets_for_source

if TYPE_CHECKING:
    from ..cog import Examination


PROMO_RECONCILE_MIN_INTERVAL_SECONDS = 15 * 60
PROMO_RECONCILE_EDIT_DELAY_SECONDS = 0.75
DEPRECATED_ROUTING_DELETE_DELAY = timedelta(hours=12)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _message_custom_ids(message: discord.Message) -> set[str]:
    custom_ids: set[str] = set()
    for row in getattr(message, "components", []) or []:
        for component in getattr(row, "children", []) or []:
            custom_id = getattr(component, "custom_id", None)
            if custom_id:
                custom_ids.add(str(custom_id))
    return custom_ids


def _view_custom_ids(view: discord.ui.View | None) -> set[str]:
    if view is None:
        return set()
    return {
        str(child.custom_id)
        for child in view.children
        if getattr(child, "custom_id", None)
    }


async def _sleep_after_reconcile_edit() -> None:
    await asyncio.sleep(PROMO_RECONCILE_EDIT_DELAY_SECONDS)


class PromoStartButton(discord.ui.Button["PromoIntakeView"]):
    def __init__(self) -> None:
        super().__init__(
            label="Continue",
            style=discord.ButtonStyle.primary,
            custom_id="promo_intake_start",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.cog._handle_promo_start(interaction)


class PromoBackButton(discord.ui.Button["PromoIntakeView"]):
    def __init__(self) -> None:
        super().__init__(
            label="Back",
            style=discord.ButtonStyle.secondary,
            custom_id="promo_intake_back",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.cog._handle_promo_back(interaction)


class PromoConfirmButton(discord.ui.Button["PromoIntakeView"]):
    def __init__(self) -> None:
        super().__init__(
            label="Confirm",
            style=discord.ButtonStyle.success,
            custom_id="promo_intake_confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.cog._handle_promo_confirm(interaction)


class PromoRouteChangeView(BaseTimeoutView):
    def __init__(self, cog: Examination) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Change Promotion",
        style=discord.ButtonStyle.secondary,
        custom_id="promo_intake_change_route",
    )
    async def change_route(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog._handle_promo_change_route(interaction)


class PromoRouteChangeConfirmView(BaseTimeoutView):
    def __init__(
        self,
        cog: Examination,
        *,
        invoker_id: int,
        ticket_channel_id: int,
        routing_message_id: Optional[int] = None,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.invoker_id = invoker_id
        self.ticket_channel_id = ticket_channel_id
        self.routing_message_id = routing_message_id

    async def _resolve_routing_message(self) -> Optional[discord.Message]:
        if not self.routing_message_id:
            return None
        routing_channel = await self.cog._get_routing_channel()
        if routing_channel is None:
            return None
        try:
            return await routing_channel.fetch_message(int(self.routing_message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError, ValueError):
            return None

    @discord.ui.button(
        label="Confirm Change",
        style=discord.ButtonStyle.danger,
    )
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who started this change can confirm it.", ephemeral=True)
            return
        case = self.cog._get_case(self.ticket_channel_id)
        routing_message = await self._resolve_routing_message()
        await self.cog._execute_promo_change_route(
            interaction,
            case=case,
            routing_message=routing_message,
        )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who started this change can cancel it.", ephemeral=True)
            return
        await interaction.response.edit_message(content="No changes made.", view=None)


class PromoFromClanSelect(discord.ui.Select["PromoIntakeView"]):
    def __init__(self, case: Optional[Dict[str, Any]] = None, *, register_all: bool = False) -> None:
        current = str((case or {}).get("from_clan") or "").upper()
        options = [
            discord.SelectOption(label=code, value=code, default=(not register_all and code == current))
            for code in PROMO_SOURCES
        ]
        super().__init__(
            placeholder="Choose your current clan",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="promo_intake_from_clan",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.cog._handle_promo_from_select(interaction, self.values[0])


class PromoToClanSelect(discord.ui.Select["PromoIntakeView"]):
    def __init__(self, case: Optional[Dict[str, Any]] = None, *, register_all: bool = False) -> None:
        current = str((case or {}).get("to_clan") or "").upper()
        from_clan = str((case or {}).get("from_clan") or "").upper()
        values = valid_targets_for_source(from_clan) if not register_all else ("BEC", "BEM", "BE1", "BES", "BE4", "BEH")
        if not values:
            values = ("BEH",)
        options = [
            discord.SelectOption(label=code, value=code, default=(not register_all and code == current))
            for code in values
        ]
        super().__init__(
            placeholder="Choose your promotion target",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="promo_intake_to_clan",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.cog._handle_promo_to_select(interaction, self.values[0])


class PromoTHLevelSelect(discord.ui.Select["PromoIntakeView"]):
    def __init__(self, case: Optional[Dict[str, Any]] = None, *, register_all: bool = False) -> None:
        current = (case or {}).get("th_level")
        options = [
            discord.SelectOption(label=f"TH{level}", value=str(level), default=(not register_all and current == level))
            for level in range(10, 19)  # TH10-TH18; bump upper bound when new TH ships
        ]
        super().__init__(
            placeholder="Choose your Town Hall level",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="promo_intake_th_level",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.cog._handle_promo_th_select(interaction, self.values[0])


class PromoIntakeView(BaseTimeoutView):
    def __init__(
        self,
        cog: Examination,
        *,
        case: Optional[Dict[str, Any]] = None,
        register_all: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        if register_all:
            self.add_item(PromoStartButton())
            self.add_item(PromoFromClanSelect(register_all=True))
            self.add_item(PromoToClanSelect(register_all=True))
            self.add_item(PromoTHLevelSelect(register_all=True))
            self.add_item(PromoBackButton())
            self.add_item(PromoConfirmButton())
            return
        state = str((case or {}).get("intake_state") or "pending")
        if state == "pending":
            self.add_item(PromoStartButton())
            return
        if state == "th_recovery":
            self.add_item(PromoTHLevelSelect(case))
            self.add_item(PromoBackButton())
            return
        if state == "selecting_from":
            self.add_item(PromoFromClanSelect(case))
            return
        if state == "selecting_to":
            self.add_item(PromoToClanSelect(case))
            self.add_item(PromoBackButton())
            return
        if state == "confirming":
            self.add_item(PromoConfirmButton())
            self.add_item(PromoBackButton())


class ExaminationPromoIntakeMixin:
    def _ensure_promo_intake_defaults(self, case: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in default_case_fields().items():
            case.setdefault(key, value)
        return case

    def _promo_case_for_interaction(self, interaction: discord.Interaction) -> Optional[Dict[str, Any]]:
        if interaction.channel_id is None:
            return None
        case = self._get_case(interaction.channel_id)
        if not case or case.get("type") != "clan_promo":
            return None
        self._ensure_promo_intake_defaults(case)
        if interaction.message and case.get("intake_message_id") and interaction.message.id != case.get("intake_message_id"):
            return None
        return case

    @staticmethod
    def _promo_is_opener(interaction: discord.Interaction, case: Dict[str, Any]) -> bool:
        return bool(case.get("opener_id")) and interaction.user.id == case.get("opener_id")

    def _touch_promo_case(self, case: Dict[str, Any]) -> None:
        now_iso = _now_iso()
        case["intake_started_at"] = case.get("intake_started_at") or now_iso
        case["intake_last_interaction_at"] = now_iso

    def _promo_intro_text(self) -> str:
        return (
            "I'll ask a few questions about your promotion, then send the request "
            "to the right team. Select **Continue** when you're ready."
        )

    def _promo_from_text(self) -> str:
        return "Which clan are you currently in?"

    def _promo_to_text(self) -> str:
        return "Which clan do you want to promote to?"

    def _promo_th_text(self) -> str:
        return "Choose your Town Hall level below. I couldn't read it from the ticket."

    def _promo_confirmation_text(self, case: Dict[str, Any]) -> str:
        from_clan = case.get("from_clan") or "Unknown"
        to_clan = case.get("to_clan") or "Unknown"
        th_level = case.get("th_level") or "?"
        return f"Confirm your promotion from **{from_clan}** to **{to_clan}** at **TH{th_level}**."

    def _promo_exam_handoff_text(self) -> str:
        return "This promotion requires an exam. Add your availability next so an examiner can find a time with you."

    def _promo_no_exam_handoff_text(self) -> str:
        return "Your request will go to leadership for review."

    def _promo_reminder_text(self, opener_mention: str) -> str:
        return (
            f"{opener_mention} your promotion request still needs one more step. "
            "Return to the ticket to continue."
        )

    def _build_promo_intake_content(
        self,
        case: Dict[str, Any],
    ) -> str:
        state = str(case.get("intake_state") or "pending")
        if state == "pending":
            return self._promo_intro_text()
        if state == "th_recovery":
            return self._promo_th_text()
        if state == "selecting_from":
            return self._promo_from_text()
        if state == "selecting_to":
            return self._promo_to_text()
        if state == "confirming":
            return self._promo_confirmation_text(case)
        if state == "complete":
            if case.get("exam_required"):
                return self._promo_exam_handoff_text()
            return self._promo_no_exam_handoff_text()
        return self._promo_intro_text()

    def _build_promo_intake_view(
        self,
        case: Optional[Dict[str, Any]] = None,
        *,
        register_all: bool = False,
    ) -> Optional[discord.ui.View]:
        if case and case.get("intake_state") == "complete" and not register_all:
            return PromoRouteChangeView(self)
        return PromoIntakeView(self, case=case, register_all=register_all)

    def _can_change_promo_route(self, interaction: discord.Interaction, case: Dict[str, Any]) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        opener_id = case.get("opener_id")
        if opener_id and interaction.user.id == opener_id:
            return True
        return self._has_exam_permissions(interaction.user)

    def _build_route_changed_embed(self, case: Dict[str, Any], ticket_channel: discord.TextChannel) -> discord.Embed:
        previous_route = case.get("route_summary")
        if not previous_route and case.get("from_clan") and case.get("to_clan"):
            previous_route = f"{case.get('from_clan')} -> {case.get('to_clan')}"
        embed = discord.Embed(
            title="Promotion Request Changed",
            description="The promotion request is being updated in the ticket, so this review is now closed.",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.add_field(name="Ticket", value=ticket_channel.mention, inline=False)
        if case.get("opener_id"):
            embed.add_field(name="Applicant", value=f"<@{case['opener_id']}>", inline=False)
        embed.add_field(name="Previous Promotion", value=previous_route or "Not provided", inline=False)
        return embed

    def _schedule_deprecated_routing_message_delete(
        self,
        routing_message: discord.Message,
        ticket_channel: discord.TextChannel,
    ) -> None:
        queue = self.state.setdefault("deprecated_routing_messages", [])
        if not isinstance(queue, list):
            queue = []
            self.state["deprecated_routing_messages"] = queue

        message_id = int(routing_message.id)
        channel_id = int(routing_message.channel.id)
        queue[:] = [
            entry
            for entry in queue
            if not (
                isinstance(entry, dict)
                and entry.get("message_id") == message_id
                and entry.get("channel_id") == channel_id
            )
        ]
        queue.append(
            {
                "channel_id": channel_id,
                "message_id": message_id,
                "ticket_channel_id": int(ticket_channel.id),
                "delete_at": (datetime.now(timezone.utc) + DEPRECATED_ROUTING_DELETE_DELAY).isoformat(),
            }
        )
        self._save()

    async def _retire_routing_message_for_route_change(
        self,
        case: Dict[str, Any],
        ticket_channel: discord.TextChannel,
        *,
        source_message: Optional[discord.Message] = None,
    ) -> None:
        routing_message_id = case.get("routing_message_id")
        if not routing_message_id and source_message is None:
            return
        routing_message = source_message
        if routing_message is None:
            routing_channel = await self._get_routing_channel()
            if not routing_channel:
                return
            try:
                routing_message = await routing_channel.fetch_message(int(routing_message_id))
            except (TypeError, ValueError, discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        try:
            await routing_message.edit(
                content=None,
                embed=self._build_route_changed_embed(case, ticket_channel),
                view=None,
            )
            self._schedule_deprecated_routing_message_delete(routing_message, ticket_channel)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            self.logger.debug(
                "Could not mark old promotion routing message as changed: channel_id=%s message_id=%s",
                ticket_channel.id,
                getattr(routing_message, "id", routing_message_id),
                exc_info=True,
            )

    async def _retire_availability_prompt_for_route_change(
        self,
        channel: discord.TextChannel,
        case: Dict[str, Any],
    ) -> None:
        prompt_id = case.get("availability_prompt_id")
        case["availability_prompt_id"] = None
        case["availability"] = ""
        case["availability_windows"] = []
        case["availability_structured"] = None
        case["availability_draft"] = {}
        case["availability_set_at"] = None
        case["availability_set_by"] = None
        self._save()
        if not prompt_id:
            return
        try:
            msg = await channel.fetch_message(int(prompt_id))
        except (TypeError, ValueError, discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        try:
            await msg.delete()
            return
        except discord.NotFound:
            return
        except (discord.Forbidden, discord.HTTPException):
            pass
        embed = discord.Embed(
            title="Closed",
            description="This step is no longer active.",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        try:
            await msg.edit(content=None, embed=embed, view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    def _reset_case_for_route_change(self, case: Dict[str, Any], user_id: int) -> None:
        case["from_clan"] = None
        case["to_clan"] = None
        case["route_summary"] = None
        case["exam_required"] = None
        case["intake_state"] = "selecting_from"
        case["intake_completed_at"] = None
        case["intake_last_interaction_at"] = _now_iso()
        case["routing_message_id"] = None
        case["routing_inflight"] = False
        case["pinged_ids"] = []
        case["pinged_role_ids"] = []
        case["stage"] = "pending"
        case["responded"] = False
        case["applicant_notified"] = False
        case["used_fallback"] = False
        case["field_refreshes"] = 0
        case["route_change_requested_at"] = _now_iso()
        case["route_change_requested_by"] = user_id

    def _reset_case_review_state(self, case: Dict[str, Any], user_id: int) -> None:
        case["routing_message_id"] = None
        case["routing_inflight"] = False
        case["pinged_ids"] = []
        case["pinged_role_ids"] = []
        case["stage"] = "pending"
        case["responded"] = False
        case["applicant_notified"] = False
        case["used_fallback"] = False
        case["field_refreshes"] = 0
        case["route_change_requested_at"] = _now_iso()
        case["route_change_requested_by"] = user_id

    async def _handle_promo_change_route(
        self,
        interaction: discord.Interaction,
        *,
        case: Optional[Dict[str, Any]] = None,
        routing_message: Optional[discord.Message] = None,
    ) -> None:
        await self._prompt_promo_change_route_confirmation(
            interaction,
            case=case,
            routing_message=routing_message,
        )

    async def _prompt_promo_change_route_confirmation(
        self,
        interaction: discord.Interaction,
        *,
        case: Optional[Dict[str, Any]] = None,
        routing_message: Optional[discord.Message] = None,
    ) -> None:
        if case is None:
            if interaction.channel_id is None:
                await interaction.response.send_message("This ticket is no longer available.", ephemeral=True)
                return
            case = self._get_case(interaction.channel_id)
        if not case or case.get("type") != "clan_promo":
            await interaction.response.send_message("This promotion request is no longer available.", ephemeral=True)
            return
        if not self._can_change_promo_route(interaction, case):
            await interaction.response.send_message("Only the applicant or leadership can change this promotion.", ephemeral=True)
            return
        ticket_channel_id = case.get("ticket_channel_id") or interaction.channel_id
        ticket_channel = self.bot.get_channel(int(ticket_channel_id)) if ticket_channel_id else None
        if not isinstance(ticket_channel, discord.TextChannel):
            await interaction.response.send_message("The ticket channel is no longer available.", ephemeral=True)
            return

        view = PromoRouteChangeConfirmView(
            self,
            invoker_id=interaction.user.id,
            ticket_channel_id=ticket_channel.id,
            routing_message_id=getattr(routing_message, "id", None),
        )
        await interaction.response.send_message(
            "Reopen the promotion questions in the ticket and close this review?",
            view=view,
            ephemeral=True,
        )

    async def _execute_promo_change_route(
        self,
        interaction: discord.Interaction,
        *,
        case: Optional[Dict[str, Any]] = None,
        routing_message: Optional[discord.Message] = None,
    ) -> None:
        if case is None:
            if interaction.channel_id is None:
                await interaction.response.send_message("This ticket is no longer available.", ephemeral=True)
                return
            case = self._get_case(interaction.channel_id)
        if not case or case.get("type") != "clan_promo":
            await interaction.response.send_message("This promotion request is no longer available.", ephemeral=True)
            return
        if not self._can_change_promo_route(interaction, case):
            await interaction.response.send_message("Only the applicant or leadership can change this promotion.", ephemeral=True)
            return
        ticket_channel_id = case.get("ticket_channel_id") or interaction.channel_id
        ticket_channel = self.bot.get_channel(int(ticket_channel_id)) if ticket_channel_id else None
        if not isinstance(ticket_channel, discord.TextChannel):
            await interaction.response.send_message("The ticket channel is no longer available.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self._retire_routing_message_for_route_change(
            case,
            ticket_channel,
            source_message=routing_message,
        )
        await self._retire_availability_prompt_for_route_change(ticket_channel, case)
        self._reset_case_for_route_change(case, interaction.user.id)
        self._pending_ticket_retries.pop(ticket_channel.id, None)
        self._pending_ticket_notified.discard(ticket_channel.id)
        self._pending_ticket_failed.discard(ticket_channel.id)
        self._save()

        await self._render_promo_intake_message(ticket_channel, case)
        await interaction.followup.send(
            "The promotion questions are open again in the ticket, and this review is closed.",
            ephemeral=True,
        )

    async def _execute_leadership_promo_route_change(
        self,
        interaction: discord.Interaction,
        *,
        ticket_channel_id: int,
        routing_message_id: int,
        from_clan: str,
        to_clan: str,
    ) -> None:
        case = self._get_case(ticket_channel_id)
        if not case or case.get("type") != "clan_promo":
            await interaction.response.send_message("This promotion request is no longer available.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not self._has_exam_permissions(interaction.user):
            await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
            return
        from_clan = str(from_clan or "").upper()
        to_clan = str(to_clan or "").upper()
        if not is_valid_route(from_clan, to_clan):
            await interaction.response.send_message("That promotion isn't available from the selected clan.", ephemeral=True)
            return
        ticket_channel = self.bot.get_channel(ticket_channel_id)
        if not isinstance(ticket_channel, discord.TextChannel):
            await interaction.response.send_message("The ticket channel is no longer available.", ephemeral=True)
            return

        routing_message = None
        routing_channel = await self._get_routing_channel()
        if routing_channel:
            try:
                routing_message = await routing_channel.fetch_message(int(routing_message_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError, ValueError):
                routing_message = None

        await interaction.response.edit_message(content="Updating the promotion request...", view=None)
        await self._retire_routing_message_for_route_change(
            case,
            ticket_channel,
            source_message=routing_message,
        )
        await self._retire_availability_prompt_for_route_change(ticket_channel, case)
        apply_completed_route(case, from_clan, to_clan)
        case["intake_state"] = "complete"
        case["intake_completed_at"] = _now_iso()
        case["intake_last_interaction_at"] = _now_iso()
        self._reset_case_review_state(case, interaction.user.id)
        self._pending_ticket_retries.pop(ticket_channel.id, None)
        self._pending_ticket_notified.discard(ticket_channel.id)
        self._pending_ticket_failed.discard(ticket_channel.id)
        self._save()

        await self._render_promo_intake_message(ticket_channel, case)
        await self.route_ticket(ticket_channel, "clan_promo")
        updated_case = self._get_case(ticket_channel.id)
        if updated_case and updated_case.get("exam_required") is False:
            await self._update_promo_intake_message(ticket_channel.id)
        await interaction.followup.send("Promotion request updated.", ephemeral=True)

    async def _reconcile_promo_route_controls(self) -> None:
        lock = getattr(self, "_promo_reconcile_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._promo_reconcile_lock = lock
        if lock.locked():
            return

        now = time.monotonic()
        last_ts = float(getattr(self, "_promo_reconcile_last_ts", 0.0) or 0.0)
        if last_ts and now - last_ts < PROMO_RECONCILE_MIN_INTERVAL_SECONDS:
            return
        self._promo_reconcile_last_ts = now

        async with lock:
            await self._reconcile_promo_route_controls_unlocked()

    async def _reconcile_promo_route_controls_unlocked(self) -> None:
        cases = self._get_cases()
        if not cases:
            return
        from ..availability.view import AvailabilityPromptView
        from ..routing.view import ExamRoutingView

        routing_channel = None
        for case in list(cases.values()):
            if not isinstance(case, dict) or case.get("type") != "clan_promo":
                continue
            ticket_channel_id = case.get("ticket_channel_id")
            ticket_channel = self.bot.get_channel(int(ticket_channel_id)) if ticket_channel_id else None
            if not isinstance(ticket_channel, discord.TextChannel):
                continue

            if case.get("intake_state") == "complete":
                intake_message_id = case.get("intake_message_id")
                if intake_message_id:
                    try:
                        msg = await ticket_channel.fetch_message(int(intake_message_id))
                        view = PromoRouteChangeView(self)
                        if _message_custom_ids(msg) != _view_custom_ids(view):
                            await msg.edit(view=view)
                            await _sleep_after_reconcile_edit()
                    except (TypeError, ValueError, discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass

            if case.get("exam_required") is True:
                prompt_id = case.get("availability_prompt_id")
                if prompt_id:
                    try:
                        opener = None
                        opener_id = case.get("opener_id")
                        if opener_id and ticket_channel.guild:
                            opener = ticket_channel.guild.get_member(opener_id)
                        msg = await ticket_channel.fetch_message(int(prompt_id))
                        title = msg.embeds[0].title if msg.embeds else ""
                        if title == "Availability Saved":
                            if _message_custom_ids(msg):
                                await msg.edit(
                                    embed=self._build_availability_confirm_embed(case, opener),
                                    view=None,
                                )
                                await _sleep_after_reconcile_edit()
                        else:
                            view = AvailabilityPromptView(self, ticket_channel.id)
                            if _message_custom_ids(msg) != _view_custom_ids(view):
                                await msg.edit(
                                    embed=self._build_availability_prompt_embed(case, opener),
                                    view=view,
                                )
                                await _sleep_after_reconcile_edit()
                    except (TypeError, ValueError, discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass

            routing_message_id = case.get("routing_message_id")
            if routing_message_id:
                if routing_channel is None:
                    routing_channel = await self._get_routing_channel()
                if routing_channel:
                    try:
                        msg = await routing_channel.fetch_message(int(routing_message_id))
                        view = ExamRoutingView(
                            self,
                            ticket_type="clan_promo",
                            exam_required=case.get("exam_required", True),
                        )
                        if _message_custom_ids(msg) != _view_custom_ids(view):
                            await msg.edit(view=view)
                            await _sleep_after_reconcile_edit()
                    except (TypeError, ValueError, discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass

    async def _delete_promo_reminder_message(
        self,
        channel: discord.TextChannel,
        case: Dict[str, Any],
    ) -> None:
        reminder_id = case.get("intake_reminder_message_id")
        if not reminder_id:
            return
        case["intake_reminder_message_id"] = None
        self._save()
        try:
            msg = await channel.fetch_message(reminder_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        try:
            await msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    async def _render_promo_intake_message(
        self,
        channel: discord.TextChannel,
        case: Dict[str, Any],
    ) -> Optional[discord.Message]:
        content = self._build_promo_intake_content(case)
        view = self._build_promo_intake_view(case)
        message_id = case.get("intake_message_id")
        if message_id:
            try:
                msg = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                msg = None
            if msg is not None:
                await msg.edit(content=content, embed=None, view=view)
                return msg
        msg = await channel.send(content=content, view=view)
        case["intake_message_id"] = msg.id
        self._save()
        return msg

    async def _ensure_promo_intake_message(
        self,
        channel: discord.TextChannel,
        case: Dict[str, Any],
    ) -> None:
        self._ensure_promo_intake_defaults(case)
        if not case.get("intake_started_at"):
            case["intake_started_at"] = _now_iso()
        if not case.get("intake_last_interaction_at"):
            case["intake_last_interaction_at"] = case["intake_started_at"]
        self._save()
        await self._render_promo_intake_message(channel, case)

    async def _update_promo_intake_message(self, channel_id: int) -> None:
        case = self._get_case(channel_id)
        if not case or case.get("type") != "clan_promo":
            return
        self._ensure_promo_intake_defaults(case)
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        await self._render_promo_intake_message(channel, case)

    async def _edit_promo_interaction_message(
        self,
        interaction: discord.Interaction,
        case: Dict[str, Any],
    ) -> None:
        content = self._build_promo_intake_content(case)
        view = self._build_promo_intake_view(case)
        await interaction.response.edit_message(content=content, embed=None, view=view)

    async def _handle_promo_start(self, interaction: discord.Interaction) -> None:
        case = self._promo_case_for_interaction(interaction)
        if not case or not self._promo_is_opener(interaction, case):
            await interaction.response.send_message("Only the applicant can complete these steps.", ephemeral=True)
            return
        if str(case.get("intake_state") or "pending") != "pending":
            await interaction.response.defer()
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.defer()
            return
        await self._delete_promo_reminder_message(channel, case)
        self._touch_promo_case(case)
        case["intake_state"] = "selecting_from"
        self._save()
        await self._edit_promo_interaction_message(interaction, case)

    async def _handle_promo_th_select(self, interaction: discord.Interaction, value: str) -> None:
        case = self._promo_case_for_interaction(interaction)
        if not case or not self._promo_is_opener(interaction, case):
            await interaction.response.send_message("Only the applicant can complete these steps.", ephemeral=True)
            return
        if case.get("intake_state") != "th_recovery":
            await interaction.response.defer()
            return
        try:
            th_level = int(value)
        except (TypeError, ValueError):
            await interaction.response.defer()
            return
        if th_level < 10 or th_level > 18:
            await interaction.response.defer()
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.defer()
            return
        await self._delete_promo_reminder_message(channel, case)
        case["th_level"] = th_level
        case["th_level_source"] = "wizard"
        case["intake_state"] = "confirming"
        self._touch_promo_case(case)
        self._save()
        await self._edit_promo_interaction_message(interaction, case)

    async def _handle_promo_from_select(self, interaction: discord.Interaction, value: str) -> None:
        case = self._promo_case_for_interaction(interaction)
        if not case or not self._promo_is_opener(interaction, case):
            await interaction.response.send_message("Only the applicant can complete these steps.", ephemeral=True)
            return
        if case.get("intake_state") != "selecting_from":
            await interaction.response.defer()
            return
        from_clan = str(value or "").upper()
        if from_clan not in PROMO_SOURCES:
            await interaction.response.defer()
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.defer()
            return
        await self._delete_promo_reminder_message(channel, case)
        case["from_clan"] = from_clan
        case["to_clan"] = None
        case["route_summary"] = None
        case["exam_required"] = None
        case["intake_state"] = "selecting_to"
        self._touch_promo_case(case)
        self._save()
        await self._edit_promo_interaction_message(interaction, case)

    async def _handle_promo_to_select(self, interaction: discord.Interaction, value: str) -> None:
        case = self._promo_case_for_interaction(interaction)
        if not case or not self._promo_is_opener(interaction, case):
            await interaction.response.send_message("Only the applicant can complete these steps.", ephemeral=True)
            return
        if case.get("intake_state") != "selecting_to":
            await interaction.response.defer()
            return
        from_clan = str(case.get("from_clan") or "").upper()
        to_clan = str(value or "").upper()
        if not is_valid_route(from_clan, to_clan):
            await interaction.response.defer()
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.defer()
            return
        await self._delete_promo_reminder_message(channel, case)
        apply_completed_route(case, from_clan, to_clan)
        case["intake_state"] = "th_recovery" if not case.get("th_level") else "confirming"
        self._touch_promo_case(case)
        self._save()
        await self._edit_promo_interaction_message(interaction, case)

    async def _handle_promo_back(self, interaction: discord.Interaction) -> None:
        case = self._promo_case_for_interaction(interaction)
        if not case or not self._promo_is_opener(interaction, case):
            await interaction.response.send_message("Only the applicant can complete these steps.", ephemeral=True)
            return
        state = str(case.get("intake_state") or "")
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.defer()
            return
        await self._delete_promo_reminder_message(channel, case)
        if state == "selecting_to":
            case["intake_state"] = "selecting_from"
        elif state == "confirming":
            case["intake_state"] = "th_recovery" if case.get("th_level_source") == "wizard" else "selecting_to"
        elif state == "th_recovery":
            case["intake_state"] = "selecting_to"
        else:
            await interaction.response.defer()
            return
        self._touch_promo_case(case)
        self._save()
        await self._edit_promo_interaction_message(interaction, case)

    async def _handle_promo_confirm(self, interaction: discord.Interaction) -> None:
        case = self._promo_case_for_interaction(interaction)
        if not case or not self._promo_is_opener(interaction, case):
            await interaction.response.send_message("Only the applicant can complete these steps.", ephemeral=True)
            return
        if case.get("intake_state") != "confirming":
            await interaction.response.defer()
            return
        from_clan = str(case.get("from_clan") or "").upper()
        to_clan = str(case.get("to_clan") or "").upper()
        if not is_valid_route(from_clan, to_clan):
            await interaction.response.send_message("That promotion is no longer available. Go back and choose again.", ephemeral=True)
            return
        case["route_summary"] = route_summary(from_clan, to_clan)
        case["exam_required"] = requires_exam(from_clan, to_clan)
        case["intake_state"] = "complete"
        case["intake_completed_at"] = _now_iso()
        self._touch_promo_case(case)
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.defer()
            return
        await self._delete_promo_reminder_message(channel, case)
        self._save()
        await interaction.response.edit_message(
            content=self._build_promo_intake_content(case),
            embed=None,
            view=None,
        )
        await self.route_ticket(channel, "clan_promo")
        updated_case = self._get_case(channel.id)
        if updated_case and updated_case.get("exam_required") is False:
            await self._update_promo_intake_message(channel.id)

    async def _maybe_send_promo_intake_reminder(
        self,
        case: Dict[str, Any],
        ticket_channel: discord.TextChannel,
        *,
        now: datetime,
    ) -> None:
        if case.get("type") != "clan_promo":
            return
        state = str(case.get("intake_state") or "pending")
        if state == "complete":
            return
        last_interaction = case.get("intake_last_interaction_at") or case.get("intake_started_at")
        if not last_interaction:
            return
        try:
            last_dt = datetime.fromisoformat(str(last_interaction))
        except ValueError:
            return
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        else:
            last_dt = last_dt.astimezone(timezone.utc)
        age = now - last_dt
        reminder_count = int(case.get("intake_reminder_count") or 0)
        if reminder_count == 0 and age < timedelta(minutes=30):
            return
        if reminder_count == 1 and age < timedelta(hours=12):
            return
        if reminder_count >= 2:
            return
        await self._delete_promo_reminder_message(ticket_channel, case)
        opener = ticket_channel.guild.get_member(case.get("opener_id")) if ticket_channel.guild else None
        opener_mention = opener.mention if opener else "there"
        reminder = await ticket_channel.send(content=self._promo_reminder_text(opener_mention))
        case["intake_reminder_message_id"] = reminder.id
        case["intake_last_reminder_at"] = _now_iso()
        case["intake_reminder_count"] = reminder_count + 1
        self._save()

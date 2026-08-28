"""Follow-up loop and applicant reminder logic."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands

from elbow_helper.configuration.channels import EXAMINATION_ROOM, EXAMINATION_TICKET_CATEGORY

from .config import FALLBACK_DELAY
from .config import RESPONSE_DELAY



class ExaminationFollowupMixin:
    @staticmethod
    def _parse_case_datetime(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    async def _latest_human_message(self, channel: discord.TextChannel) -> Optional[discord.Message]:
        # Return the most recent non-bot message in a ticket.
        try:
            async for entry in channel.history(limit=30, oldest_first=False):
                if entry.author.bot:
                    continue
                return entry
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    async def _waiting_since(self, case: Dict[str, Any], ticket_channel: discord.TextChannel) -> Optional[datetime]:
        # Determine when wait time started.
        # Baseline is routing creation; a newer applicant message overrides it.
        baseline = self._parse_case_datetime(case.get("routing_created_at")) or self._parse_case_datetime(case.get("created_at"))
        opener_id = case.get("opener_id")
        latest = await self._latest_human_message(ticket_channel)
        if not latest or not opener_id or latest.author.id != opener_id:
            return baseline
        latest_at = latest.created_at
        if latest_at.tzinfo is None:
            latest_dt = latest_at.replace(tzinfo=timezone.utc)
        else:
            latest_dt = latest_at.astimezone(timezone.utc)
        if baseline is None or latest_dt > baseline:
            return latest_dt
        return baseline

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Kick off routing as soon as the TicketTool embed arrives.
        if message.guild and isinstance(message.channel, discord.TextChannel):
            if message.channel.category_id == EXAMINATION_TICKET_CATEGORY:
                ticket_type = self._message_ticket_type(message)
                if ticket_type:
                    opener = await self.find_ticket_opener(message.channel)
                    if message.channel.name.startswith("ticket-"):
                        await self._rename_ticket_channel(message.channel, ticket_type, opener)
                    case = self._ensure_case_entry(message.channel, ticket_type, opener)
                    if not case.get("routing_message_id") and not case.get("routing_inflight"):
                        case["routing_inflight"] = True
                        self._save()
                        await self.route_ticket(message.channel, ticket_type)

        # Mark cases as responded when examiners or leadership reply in routing or in the ticket itself.
        if not message.guild or message.author.bot:
            return
        cases = self._get_cases()
        updated = False

        if isinstance(message.channel, discord.TextChannel) and message.channel.category_id == EXAMINATION_TICKET_CATEGORY:
            case = self._get_case(message.channel.id)
            opener_id = case.get("opener_id") if case else None
            if case and opener_id and message.author.id != opener_id and not case.get("responded"):
                case["responded"] = True
                updated = True

        if message.channel.id != EXAMINATION_ROOM:
            if updated:
                self._save()
            return

        if not isinstance(message.author, discord.Member):
            if updated:
                self._save()
            return
        if not self._has_exam_permissions(message.author):
            if updated:
                self._save()
            return

        reply_to_id = None
        if message.reference:
            reply_to_id = message.reference.message_id
        ticket_mentions = {
            int(ticket_id)
            for ticket_id in re.findall(r"<#(\d+)>", message.content or "")
        }
        for case in cases.values():
            if case.get("responded"):
                continue
            routing_msg_id = case.get("routing_message_id")
            ticket_channel_id = case.get("ticket_channel_id")
            if reply_to_id and routing_msg_id == reply_to_id:
                case["responded"] = True
                updated = True
                continue
            if ticket_channel_id and ticket_channel_id in ticket_mentions:
                case["responded"] = True
                updated = True

        if updated:
            self._save()

    async def _followup_loop(self):
        # Re-ping after 18h, then widen to +2 TH after 24h.
        await self.bot.wait_until_ready()
        while True:
            await asyncio.sleep(600)
            cases = self._get_cases()
            if not cases:
                continue
            routing_channel = await self._get_routing_channel()
            if not routing_channel:
                continue
            now = datetime.now(timezone.utc)
            for case_id, case in list(cases.items()):
                ticket_channel_id = case.get("ticket_channel_id")
                ticket_channel = None
                if ticket_channel_id:
                    ticket_channel = self.bot.get_channel(ticket_channel_id)
                    if not ticket_channel:
                        try:
                            fetched = await self.bot.fetch_channel(ticket_channel_id)
                            if isinstance(fetched, discord.TextChannel):
                                ticket_channel = fetched
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            ticket_channel = None
                if not ticket_channel:
                    # Stop follow-ups for deleted or missing tickets.
                    routing_msg_id = case.get("routing_message_id")
                    if routing_msg_id:
                        try:
                            msg = await routing_channel.fetch_message(routing_msg_id)
                            await msg.delete()
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass
                    cases.pop(case_id, None)
                    self._save()
                    continue
                if case.get("type") == "clan_promo" and case.get("intake_state") != "complete":
                    await self._update_promo_intake_message(ticket_channel.id)
                    await self._maybe_send_promo_intake_reminder(case, ticket_channel, now=now)
                    continue
                if case.get("responded"):
                    continue
                waiting_since = await self._waiting_since(case, ticket_channel)
                if waiting_since is None:
                    # No pending applicant message; wait for applicant to respond.
                    continue
                waiting_since_iso = waiting_since.isoformat()
                if case.get("wait_started_at") != waiting_since_iso:
                    case["wait_started_at"] = waiting_since_iso
                    if case.get("stage") in {"initial", "reminder", "fallback"}:
                        # Reset cycle when a new applicant message starts a fresh wait.
                        case["stage"] = "initial"
                        case["pinged_ids"] = []
                        case["pinged_role_ids"] = []
                        case["used_fallback"] = False
                    self._save()
                case_age = now - waiting_since
                if case_age >= FALLBACK_DELAY and case.get("stage") in {"initial", "reminder"}:
                    await self._send_followup(case, routing_channel, fallback=True)
                    case["stage"] = "fallback"
                    case["last_ping_at"] = now.isoformat()
                    self._save()
                elif case_age >= RESPONSE_DELAY and case.get("stage") == "initial":
                    await self._send_followup(case, routing_channel, fallback=False)
                    case["stage"] = "reminder"
                    case["last_ping_at"] = now.isoformat()
                    self._save()

    async def _send_followup(
        self,
        case: Dict[str, Any],
        routing_channel: discord.TextChannel,
        *,
        fallback: bool,
    ) -> None:
        # Follow-up ping with optional +2 TH fallback.
        guild = routing_channel.guild
        examiners = self._collect_examiners(guild)
        roster = self._get_examiner_roster()
        ignore_ids = set(case.get("pinged_ids", [])) if fallback else set()
        th_level = case.get("th_level")
        availability = case.get("availability", "")
        clan_codes = case.get("clan_codes") or []
        pinged: List[discord.Member] = []
        applicant_windows = case.get("availability_structured") or case.get("availability_windows") or []

        if case.get("type") == "clan_promo":
            if case.get("exam_required", True) and availability and th_level:
                pinged, _ = self._select_clan_promo_examiners(
                    examiners,
                    roster,
                    th_level,
                    availability,
                    applicant_windows=applicant_windows or None,
                    ignore_ids=ignore_ids,
                )
        else:
            pinged = self._match_examiners(
                examiners,
                roster,
                None,
                availability if availability else "",
                applicant_windows=applicant_windows or None,
                clan_codes=clan_codes,
                ignore_ids=ignore_ids,
            )

        pinged_role_ids, _ = self._resolve_notification_targets(
            case.get("type"),
            clan_codes,
            pinged,
            exam_required=case.get("exam_required", True),
        )
        mentions = self._build_notification_mentions(
            case.get("type"),
            clan_codes,
            pinged,
            exam_required=case.get("exam_required", True),
        )

        content = f"{mentions} Still waiting for your response." if mentions else "Still waiting for a response."
        ticket_channel_id = case.get("ticket_channel_id")
        content += f" Ticket: <#{ticket_channel_id}>" if ticket_channel_id else ""
        routing_msg = None
        routing_msg_id = case.get("routing_message_id")
        if routing_msg_id:
            try:
                routing_msg = await routing_channel.fetch_message(routing_msg_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                routing_msg = None
        if routing_msg:
            await routing_msg.reply(content=content, mention_author=False)
        else:
            await routing_channel.send(content=content)
        case["pinged_ids"] = list({*ignore_ids, *[m.id for m in pinged]})
        case["pinged_role_ids"] = pinged_role_ids

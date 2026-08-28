"""Ticket routing orchestration and retry flow."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import discord

from elbow_helper.configuration.channels import EXAMINATION_ROOM, EXAMINATION_TICKET_CATEGORY
from elbow_helper.configuration.roles import LEAD

from ..intake.logic import apply_completed_route
from ..intake.logic import default_case_fields
from ..intake.logic import is_valid_route
from ..config import REQUIRED_FIELD_MAX_RETRIES
from ..config import REQUIRED_FIELD_RETRY_DELAY
from .fields import QUESTION_MAP
from .fields import TICKET_RENAME
from .fields import _extract_ticket_lines
from .fields import _normalize_question
from .fields import _parse_tickettool_description
from .fields import _strip_invisible
from .fields import extract_th_level
from .fields import infer_ticket_type
from .fields import parse_ticket_fields

class ExaminationRoutingFlowMixin:
    def _ensure_case_entry(
        self,
        channel: discord.TextChannel,
        ticket_type: str,
        opener: Optional[discord.Member],
    ) -> Dict[str, Any]:
        # Create a minimal case entry to track availability before routing.
        cases = self._get_cases()
        key = str(channel.id)
        case = cases.get(key)
        if case:
            case.setdefault("pinged_role_ids", [])
            if ticket_type == "clan_promo":
                self._ensure_promo_intake_defaults(case)
            else:
                case.setdefault("exam_required", True)
            return case
        case = {
            "type": ticket_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "opener_id": opener.id if opener else None,
            "ticket_channel_id": channel.id,
            "availability": "",
            "availability_windows": [],
            "availability_structured": None,
            "availability_prompt_id": None,
            "availability_draft": {},
            "availability_set_at": None,
            "availability_set_by": None,
            "routing_message_id": None,
            "routing_inflight": False,
            "pinged_ids": [],
            "pinged_role_ids": [],
            "exam_required": True,
            "stage": "pending",
            "responded": False,
            "applicant_notified": False,
            "used_fallback": False,
            "field_refreshes": 0,
        }
        if ticket_type == "clan_promo":
            case.update(default_case_fields())
        cases[key] = case
        self._save()
        return case
    async def _get_routing_channel(self) -> Optional[discord.abc.Messageable]:
        channel = self.bot.get_channel(EXAMINATION_ROOM)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        try:
            fetched = await self.bot.fetch_channel(EXAMINATION_ROOM)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, (discord.TextChannel, discord.Thread)) else None
    async def detect_ticket_type(self, channel: discord.TextChannel) -> Optional[str]:
        # Inspect channel metadata and early messages for TicketTool triggers.
        guess = infer_ticket_type(channel.topic or "")
        if guess:
            return guess
        guess = infer_ticket_type(channel.name or "")
        if guess:
            return guess
        try:
            async for msg in channel.history(limit=12, oldest_first=True):
                parts: List[str] = [msg.content]
                for emb in msg.embeds:
                    if emb.title:
                        parts.append(emb.title)
                    if emb.description:
                        parts.append(emb.description)
                    for field in emb.fields:
                        if field.name:
                            parts.append(field.name)
                        if field.value:
                            parts.append(field.value)
                guess = infer_ticket_type(" ".join(filter(None, parts)))
                if guess:
                    return guess
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    async def find_ticket_opener(self, channel: discord.TextChannel) -> Optional[discord.Member]:
        # Prefer first mention, else first human author.
        try:
            async for msg in channel.history(limit=25, oldest_first=True):
                if msg.mentions:
                    member = channel.guild.get_member(msg.mentions[0].id)
                    if member:
                        return member
                if msg.embeds:
                    emb_text = []
                    for emb in msg.embeds:
                        if emb.description:
                            emb_text.append(emb.description)
                        for field in emb.fields:
                            if field.value:
                                emb_text.append(field.value)
                    joined = " ".join(emb_text)
                    match = re.search(r"<@!?(\d+)>", joined)
                    if match:
                        member = channel.guild.get_member(int(match.group(1)))
                        if member:
                            return member
                if msg.author and not msg.author.bot:
                    return msg.author if isinstance(msg.author, discord.Member) else None
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    def _sanitize_username(self, opener: discord.abc.User) -> str:
        # Convert a username to a safe channel suffix.
        safe_user = opener.name if hasattr(opener, "name") else str(opener)
        username = re.sub(r"[^a-zA-Z0-9]", "-", safe_user).strip("-").lower()
        return username or "user"

    async def _rename_ticket_channel(
        self,
        channel: discord.TextChannel,
        ticket_type: str,
        opener: Optional[discord.abc.User],
    ) -> None:
        # Apply the promotion ticket naming schema.
        if not opener:
            return
        label = TICKET_RENAME.get(ticket_type)
        if not label:
            return
        username = self._sanitize_username(opener)
        new_name = f"{label['emoji']}｜{label['short']}-{username}"
        if channel.name == new_name:
            return
        try:
            await channel.edit(name=new_name, reason="Rename an exam ticket")
        except (discord.Forbidden, discord.HTTPException):
            return

    async def _latest_applicant_message(self, channel: discord.TextChannel, opener: Optional[discord.Member]) -> str:
        if not opener:
            return ""
        try:
            async for msg in channel.history(limit=30, oldest_first=False):
                if msg.author and msg.author.id == opener.id and msg.content:
                    return msg.content.strip()
        except (discord.Forbidden, discord.HTTPException):
            return ""
        return ""

    async def _extract_ticket_fields(self, channel: discord.TextChannel) -> Dict[str, str]:
        # Extract answers from the TicketTool template messages.
        messages: List[discord.Message] = []
        try:
            async for msg in channel.history(limit=40, oldest_first=True):
                messages.append(msg)
        except (discord.Forbidden, discord.HTTPException):
            return {}
        fields: Dict[str, str] = {}
        for msg in messages:
            for emb in msg.embeds:
                if emb.description:
                    desc_fields = _parse_tickettool_description(emb.description)
                    for key, value in desc_fields.items():
                        fields.setdefault(key, value)
                for field in emb.fields:
                    if not field.name or not field.value:
                        continue
                    normalized = _normalize_question(field.name)
                    key = None
                    for question, mapped in QUESTION_MAP.items():
                        if normalized.startswith(question):
                            key = mapped
                            break
                    if not key:
                        continue
                    value = _strip_invisible(field.value)
                    if value:
                        fields[key] = value
        lines = _extract_ticket_lines(messages)
        line_fields = parse_ticket_fields(lines)
        for key, value in line_fields.items():
            fields.setdefault(key, value)
        return fields

    async def _wait_for_required_fields(
        self,
        channel: discord.TextChannel,
        *,
        required_keys: List[str],
        attempts: int = 3,
        delay: float = 4.0,
    ) -> Dict[str, str]:
        # Give TicketTool time to post the Q/A embed before routing.
        fields = await self._extract_ticket_fields(channel)
        for _ in range(attempts):
            if all(fields.get(key) for key in required_keys):
                return fields
            await asyncio.sleep(delay)
            fields = await self._extract_ticket_fields(channel)
        return fields

    async def _retry_route_ticket(self, channel_id: int, ticket_type: str) -> None:
        await asyncio.sleep(REQUIRED_FIELD_RETRY_DELAY)
        cases = self._get_cases()
        case = cases.get(str(channel_id))
        if case and (case.get("routing_message_id") or case.get("routing_inflight")):
            return
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        await self.route_ticket(channel, ticket_type)

    def _schedule_retry(self, channel_id: int, ticket_type: str) -> None:
        retries = self._pending_ticket_retries.get(channel_id, 0)
        if retries >= REQUIRED_FIELD_MAX_RETRIES:
            return
        self._pending_ticket_retries[channel_id] = retries + 1
        asyncio.create_task(self._retry_route_ticket(channel_id, ticket_type))

    async def _post_missing_data_alert(
        self,
        channel: discord.TextChannel,
        ticket_type: str,
        opener: Optional[discord.Member],
        availability: str,
        th_level: Optional[int],
        elder_reason: str,
        elder_war: str,
        elder_clan: str,
        clan_codes: List[str],
    ) -> None:
        case = self._get_case(channel.id)
        routing_channel = await self._get_routing_channel()
        if not routing_channel:
            if case:
                case["routing_inflight"] = False
                self._save()
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        notes = "Some required application answers are missing. Review the ticket before continuing."
        embed = self._build_routing_embed(
            ticket_type,
            ticket_mention=channel.mention,
            opener_mention=opener.mention if opener else "Unknown",
            th_level=th_level,
            from_clan="",
            to_clan="",
            elder_reason=elder_reason,
            elder_war=elder_war,
            elder_clan=elder_clan,
            clan_codes=clan_codes,
            notes=notes,
        )
        mention = " ".join(f"<@&{role_id}>" for role_id in sorted(LEAD))
        intro = "Some application answers couldn't be read. Review the ticket before continuing."
        content = f"{mention} {intro} Ticket: {channel.mention}"
        from .view import ExamRoutingView

        routing_msg = await routing_channel.send(
            content=content,
            embed=embed,
            view=ExamRoutingView(self, ticket_type=ticket_type),
        )
        case_data = {
            "type": ticket_type,
            "created_at": now_iso,
            "routing_created_at": now_iso,
            "opener_id": opener.id if opener else None,
            "ticket_channel_id": channel.id,
            "routing_message_id": routing_msg.id,
            "routing_inflight": False,
            "th_level": th_level,
            "availability": availability,
            "availability_windows": [],
            "clan_codes": clan_codes,
            "elder_reason": elder_reason,
            "elder_war": elder_war,
            "elder_clan": elder_clan,
            "pinged_ids": [],
            "pinged_role_ids": sorted(LEAD),
            "exam_required": True,
            "stage": "missing",
            "responded": False,
            "applicant_notified": False,
            "used_fallback": False,
            "field_refreshes": 0,
            "availability_structured": None,
            "availability_prompt_id": None,
            "availability_draft": {},
            "availability_set_at": None,
            "availability_set_by": None,
        }
        cases = self._get_cases()
        cases[str(channel.id)] = case_data
        self._save()

    async def route_ticket(self, channel: discord.TextChannel, ticket_type: str) -> None:
        # Main routing logic for clan or elder promotions.
        opener = await self.find_ticket_opener(channel)
        latest_message = await self._latest_applicant_message(channel, opener)
        case = self._ensure_case_entry(channel, ticket_type, opener)
        if not case.get("routing_inflight"):
            case["routing_inflight"] = True
            self._save()
        if ticket_type == "clan_promo":
            fields = await self._extract_ticket_fields(channel)
            new_th = extract_th_level(fields.get("th_level", ""))
            if new_th and case.get("th_level_source") != "wizard" and case.get("th_level") != new_th:
                case["th_level"] = new_th
                case["th_level_source"] = "initial_ticket"
                if case.get("intake_state") == "th_recovery":
                    case["intake_state"] = "confirming"
                self._save()
            if case.get("intake_state") != "complete":
                await self._ensure_promo_intake_message(channel, case)
                case["routing_inflight"] = False
                self._save()
                if not new_th and case.get("field_refreshes", 0) < 3:
                    asyncio.create_task(self._refresh_case_fields(channel.id))
                return
            from_clan = str(case.get("from_clan") or "").upper()
            to_clan = str(case.get("to_clan") or "").upper()
            if not is_valid_route(from_clan, to_clan):
                case["intake_state"] = "selecting_from" if not from_clan else "selecting_to"
                case["routing_inflight"] = False
                self._save()
                await self._ensure_promo_intake_message(channel, case)
                return
            apply_completed_route(case, from_clan, to_clan)
            availability = (case.get("availability") or "").strip()
            th_level = case.get("th_level") or new_th
            exam_required = bool(case.get("exam_required"))
            if exam_required:
                if not availability:
                    await self._ensure_availability_prompt(channel, case, opener)
                    case["routing_inflight"] = False
                    self._save()
                    return
            else:
                await self._clear_availability_prompt(channel.id)
            elder_reason = ""
            elder_war = ""
            elder_clan = ""
            clan_codes = [from_clan] if from_clan else []
        else:
            required = ["elder_reason", "elder_war", "elder_clan"]
            fields = await self._wait_for_required_fields(channel, required_keys=required)
            availability = (fields.get("availability") or "").strip()
            missing_required = [key for key in required if not fields.get(key)]
            if case.get("exam_required") is not True:
                case["exam_required"] = True
                self._save()
        if ticket_type != "clan_promo" and missing_required:
            opener_mention = opener.mention if opener else ""
            if channel.id not in self._pending_ticket_notified:
                self._pending_ticket_notified.add(channel.id)
                message_parts = []
                if any(key in missing_required for key in ("elder_reason", "elder_war", "elder_clan")):
                    message_parts.append(
                        "Reopen the application form and submit the missing answers so your promotion request can continue."
                    )
                message = " ".join(message_parts) if message_parts else "Add the missing application answers in your ticket."
                await channel.send(f"{opener_mention} {message}".strip())
            retries = self._pending_ticket_retries.get(channel.id, 0)
            if retries >= REQUIRED_FIELD_MAX_RETRIES and channel.id not in self._pending_ticket_failed:
                self._pending_ticket_failed.add(channel.id)
                await self._post_missing_data_alert(
                    channel,
                    ticket_type,
                    opener,
                    availability,
                    extract_th_level(fields.get("th_level", "")),
                    fields.get("elder_reason", ""),
                    fields.get("elder_war", ""),
                    fields.get("elder_clan", ""),
                    self._resolve_clans(opener, fields.get("elder_clan", "")),
                )
                case["routing_inflight"] = False
                self._save()
                return
            case["routing_inflight"] = False
            self._save()
            self._schedule_retry(channel.id, ticket_type)
            return
        if ticket_type != "clan_promo":
            th_text = (fields.get("th_level") or "").strip()
            th_level = extract_th_level(th_text)
            elder_reason = fields.get("elder_reason") or ""
            elder_war = fields.get("elder_war") or ""
            elder_clan = fields.get("elder_clan") or ""
            clan_codes = self._resolve_clans(opener, elder_clan or latest_message)
            exam_required = case.get("exam_required", True)

        routing_channel = await self._get_routing_channel()
        if not routing_channel:
            case["routing_inflight"] = False
            self._save()
            return

        guild = routing_channel.guild
        examiners = self._collect_examiners(guild)
        roster = self._get_examiner_roster()
        pinged_members: List[discord.Member] = []
        applicant_windows = case.get("availability_structured") or case.get("availability_windows") or []

        used_fallback = False
        if ticket_type == "clan_promo":
            if exam_required:
                pinged_members, used_fallback = self._select_clan_promo_examiners(
                    examiners,
                    roster,
                    th_level,
                    availability,
                    applicant_windows=applicant_windows or None,
                )
        else:
            matches = self._match_examiners(
                examiners,
                roster,
                None,
                availability if availability else "",
                applicant_windows=applicant_windows or None,
                clan_codes=clan_codes,
            )
            pinged_members = matches

        pinged_role_ids, visible_members = self._resolve_notification_targets(
            ticket_type,
            clan_codes,
            pinged_members,
            exam_required=exam_required,
        )
        mentions = self._build_notification_mentions(
            ticket_type,
            clan_codes,
            pinged_members,
            exam_required=exam_required,
        )
        applicant_availability = self._format_applicant_availability(
            availability or "",
            applicant_windows or [],
        )
        embed = self._build_routing_embed(
            ticket_type,
            ticket_mention=channel.mention,
            opener_mention=opener.mention if opener else "Unknown",
            th_level=th_level,
            from_clan=case.get("from_clan") or "",
            to_clan=case.get("to_clan") or "",
            elder_reason=elder_reason,
            elder_war=elder_war,
            elder_clan=elder_clan,
            clan_codes=clan_codes,
            applicant_availability=applicant_availability,
            matched_examiners=self._format_matched_examiners(visible_members),
            exam_required=exam_required,
        )

        intro = "Please review this elder promotion request." if ticket_type == "elder_promo" else "Please review this clan promotion request."
        content = f"{mentions} {intro}".strip()
        from .view import ExamRoutingView

        routing_msg = await routing_channel.send(
            content=content,
            embed=embed,
            view=ExamRoutingView(self, ticket_type=ticket_type, exam_required=exam_required),
        )

        self._pending_ticket_retries.pop(channel.id, None)
        self._pending_ticket_notified.discard(channel.id)

        now_iso = datetime.now(timezone.utc).isoformat()
        cases = self._get_cases()
        existing_case = cases.get(str(channel.id), {})
        case_data = {
            "type": ticket_type,
            "created_at": now_iso,
            "routing_created_at": now_iso,
            "opener_id": opener.id if opener else None,
            "ticket_channel_id": channel.id,
            "routing_message_id": routing_msg.id,
            "routing_inflight": False,
            "th_level": th_level,
            "availability": availability,
            "clan_codes": clan_codes,
            "from_clan": case.get("from_clan"),
            "to_clan": case.get("to_clan"),
            "route_summary": case.get("route_summary"),
            "elder_reason": elder_reason,
            "elder_war": elder_war,
            "elder_clan": elder_clan,
            "pinged_ids": [member.id for member in pinged_members],
            "pinged_role_ids": pinged_role_ids,
            "exam_required": exam_required,
            "stage": "initial",
            "responded": False,
            "applicant_notified": False,
            "used_fallback": used_fallback if ticket_type == "clan_promo" else False,
            "field_refreshes": 0,
        }
        if ticket_type == "clan_promo":
            case_data["intake_state"] = "complete"
            case_data["intake_message_id"] = case.get("intake_message_id")
            case_data["intake_started_at"] = case.get("intake_started_at")
            case_data["intake_completed_at"] = case.get("intake_completed_at")
            case_data["intake_last_interaction_at"] = case.get("intake_last_interaction_at")
            case_data["intake_last_reminder_at"] = case.get("intake_last_reminder_at")
            case_data["intake_reminder_count"] = case.get("intake_reminder_count", 0)
            case_data["intake_reminder_message_id"] = case.get("intake_reminder_message_id")
            case_data["th_level_source"] = case.get("th_level_source")
        case_data["availability_structured"] = existing_case.get("availability_structured")
        case_data["availability_windows"] = existing_case.get("availability_windows", [])
        case_data["availability_prompt_id"] = existing_case.get("availability_prompt_id")
        case_data["availability_draft"] = existing_case.get("availability_draft", {})
        case_data["availability_set_at"] = existing_case.get("availability_set_at")
        case_data["availability_set_by"] = existing_case.get("availability_set_by")
        cases[str(channel.id)] = {**existing_case, **case_data}
        self._save()
        if ticket_type != "clan_promo":
            # TicketTool can post the Q/A embed slightly later; retry once to update fields.
            asyncio.create_task(self._refresh_case_fields(channel.id))

    async def _refresh_case_fields(self, channel_id: int) -> None:
        await asyncio.sleep(8)
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        cases = self._get_cases()
        case = cases.get(str(channel_id))
        if not case or case.get("responded"):
            return
        ticket_type = case.get("type")
        required_keys = []
        if ticket_type == "clan_promo":
            required_keys = ["th_level"]
        elif ticket_type == "elder_promo":
            required_keys = ["elder_reason", "elder_war", "elder_clan"]
        case["field_refreshes"] = case.get("field_refreshes", 0) + 1
        self._save()
        fields = await self._extract_ticket_fields(channel)
        if not fields:
            missing = any(not case.get(key) for key in required_keys)
            if missing and case["field_refreshes"] < 3:
                asyncio.create_task(self._refresh_case_fields(channel_id))
            return
        updated = False
        # Availability is collected via the structured prompt; ignore TicketTool text.
        th_text = fields.get("th_level")
        new_th = extract_th_level(th_text or "")
        if new_th and new_th != case.get("th_level"):
            case["th_level"] = new_th
            if ticket_type == "clan_promo" and case.get("th_level_source") != "wizard":
                case["th_level_source"] = "initial_ticket"
                if case.get("intake_state") == "th_recovery":
                    case["intake_state"] = "confirming"
            updated = True
        if fields.get("elder_reason") and fields.get("elder_reason") != case.get("elder_reason"):
            case["elder_reason"] = fields["elder_reason"]
            updated = True
        if fields.get("elder_war") and fields.get("elder_war") != case.get("elder_war"):
            case["elder_war"] = fields["elder_war"]
            updated = True
        if fields.get("elder_clan") and fields.get("elder_clan") != case.get("elder_clan"):
            case["elder_clan"] = fields["elder_clan"]
            updated = True
        missing = any(not case.get(key) for key in required_keys)
        if not updated:
            if missing and case["field_refreshes"] < 3:
                asyncio.create_task(self._refresh_case_fields(channel_id))
            return
        if ticket_type == "clan_promo":
            self._save()
            await self._update_promo_intake_message(channel_id)
            return
        opener = channel.guild.get_member(case.get("opener_id")) if channel.guild else None
        clan_codes = self._resolve_clans(opener, case.get("elder_clan") or "")
        case["clan_codes"] = clan_codes
        self._save()
        routing_channel = await self._get_routing_channel()
        if not routing_channel:
            return
        try:
            routing_msg = await routing_channel.fetch_message(case.get("routing_message_id"))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        from .view import ExamRoutingView
        pinged_members: List[discord.Member] = []
        pinged_ids = case.get("pinged_ids") or []
        if routing_channel.guild and pinged_ids:
            for user_id in pinged_ids:
                member = routing_channel.guild.get_member(int(user_id))
                if member:
                    pinged_members.append(member)
        applicant_windows = case.get("availability_structured") or case.get("availability_windows") or []
        applicant_availability = self._format_applicant_availability(
            case.get("availability") or "",
            applicant_windows or [],
        )
        pinged_role_ids, visible_members = self._resolve_notification_targets(
            case.get("type"),
            clan_codes,
            pinged_members,
            exam_required=case.get("exam_required", True),
        )
        embed = self._build_routing_embed(
            case.get("type"),
            ticket_mention=channel.mention,
            opener_mention=opener.mention if opener else "Unknown",
            th_level=case.get("th_level"),
            from_clan=case.get("from_clan") or "",
            to_clan=case.get("to_clan") or "",
            elder_reason=case.get("elder_reason") or "",
            elder_war=case.get("elder_war") or "",
            elder_clan=case.get("elder_clan") or "",
            clan_codes=clan_codes,
            applicant_availability=applicant_availability,
            matched_examiners=self._format_matched_examiners(visible_members),
            exam_required=case.get("exam_required", True),
        )
        await routing_msg.edit(
            embed=embed,
            view=ExamRoutingView(
                self,
                ticket_type=str(case.get("type") or "clan_promo"),
                exam_required=case.get("exam_required", True),
            ),
        )
        if case.get("pinged_ids") or case.get("pinged_role_ids"):
            return
        routing_channel = await self._get_routing_channel()
        if not routing_channel:
            return
        guild = routing_channel.guild
        examiners = self._collect_examiners(guild)
        roster = self._get_examiner_roster()
        availability = case.get("availability") or ""
        th_level = case.get("th_level")
        pinged_members: List[discord.Member] = []
        used_fallback = case.get("used_fallback", False)
        if case.get("type") == "clan_promo":
            if case.get("exam_required", True):
                pinged_members, used_fallback = self._select_clan_promo_examiners(
                    examiners,
                    roster,
                    th_level,
                    availability,
                    applicant_windows=applicant_windows or None,
                )
        else:
            pinged_members = self._match_examiners(
                examiners,
                roster,
                None,
                availability if availability else "",
                applicant_windows=applicant_windows or None,
                clan_codes=clan_codes,
            )
        pinged_role_ids, visible_members = self._resolve_notification_targets(
            case.get("type"),
            clan_codes,
            pinged_members,
            exam_required=case.get("exam_required", True),
        )
        mentions = self._build_notification_mentions(
            case.get("type"),
            clan_codes,
            pinged_members,
            exam_required=case.get("exam_required", True),
        )
        if mentions:
            await routing_channel.send(
                content=f"{mentions} Ticket: {channel.mention}",
            )
        case["pinged_ids"] = [member.id for member in pinged_members]
        case["pinged_role_ids"] = pinged_role_ids
        case["used_fallback"] = used_fallback
        self._save()

    async def process_ticket_channel(self, channel: discord.TextChannel) -> None:
        # Detect and route eligible tickets.
        if channel.category_id != EXAMINATION_TICKET_CATEGORY:
            return
        ticket_type = await self.detect_ticket_type(channel)
        if not ticket_type:
            return
        opener = await self.find_ticket_opener(channel)
        await self._rename_ticket_channel(channel, ticket_type, opener)
        case = self._ensure_case_entry(channel, ticket_type, opener)
        case_updated = False
        if case.get("type") != ticket_type:
            case["type"] = ticket_type
            case_updated = True
        opener_id = opener.id if opener else None
        if opener_id and case.get("opener_id") != opener_id:
            case["opener_id"] = opener_id
            case_updated = True
        if case_updated:
            self._save()
        if case.get("routing_message_id") or case.get("routing_inflight"):
            return
        case["routing_inflight"] = True
        self._save()
        await self.route_ticket(channel, ticket_type)

    def _message_ticket_type(self, message: discord.Message) -> Optional[str]:
        # Detect TicketTool trigger text from a single message.
        parts: List[str] = [message.content]
        for emb in message.embeds:
            if emb.title:
                parts.append(emb.title)
            if emb.description:
                parts.append(emb.description)
            for field in emb.fields:
                if field.name:
                    parts.append(field.name)
                if field.value:
                    parts.append(field.value)
        return infer_ticket_type(" ".join(filter(None, parts)))


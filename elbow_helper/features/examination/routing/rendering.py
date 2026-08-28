"""Embed and notification rendering helpers for examination routing."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

import discord

from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL

from ..availability import _format_availability_display
from ..availability import _format_structured_availability_display
from ..availability import _format_structured_availability_examples
from ..availability import _format_ticket_availability_display


class ExaminationRoutingRenderingMixin:
    def _resolve_notification_targets(
        self,
        ticket_type: str,
        clan_codes: List[str],
        matched_members: List[discord.Member],
        *,
        exam_required: bool = True,
    ) -> tuple[List[int], List[discord.Member]]:
        unique_members = self._unique_members(matched_members)
        role_ids: List[int] = []
        direct_members = unique_members
        if ticket_type == "elder_promo":
            role_ids = self._resolve_leadership_role_ids(clan_codes)
            covered_member_ids = {
                member.id
                for member in unique_members
                if any(role.id in role_ids for role in member.roles)
            }
            direct_members = [
                member for member in unique_members if member.id not in covered_member_ids
            ]
        elif ticket_type == "clan_promo" and not exam_required:
            role_ids = self._resolve_leadership_role_ids(clan_codes)
            direct_members = []
        return role_ids, direct_members

    def _build_notification_mentions(
        self,
        ticket_type: str,
        clan_codes: List[str],
        matched_members: List[discord.Member],
        *,
        exam_required: bool = True,
    ) -> str:
        role_ids, direct_members = self._resolve_notification_targets(
            ticket_type,
            clan_codes,
            matched_members,
            exam_required=exam_required,
        )
        mentions = [f"<@&{role_id}>" for role_id in role_ids]
        mentions.extend(member.mention for member in direct_members)
        return " ".join(mentions).strip()

    @staticmethod
    def _format_matched_examiners(members: List[discord.Member]) -> str:
        unique_members = list({member.id: member for member in members}.values())
        if not unique_members:
            return "No examiners match this request."
        return "\n".join(member.mention for member in unique_members)

    @staticmethod
    def _format_overlap_windows(
        overlaps: List[tuple[datetime, datetime]],
        *,
        limit: int = 3,
    ) -> str:
        if not overlaps:
            return "No shared availability was found."
        limited = overlaps[:limit]
        lines = [
            f"<t:{int(start_dt.timestamp())}:F> - <t:{int(end_dt.timestamp())}:F>"
            for start_dt, end_dt in limited
        ]
        if len(overlaps) > limit:
            remaining = len(overlaps) - limit
            if remaining == 1:
                lines.append("...plus 1 more shared time")
            else:
                lines.append(f"...plus {remaining} more shared times")
        return "\n".join(lines)

    @staticmethod
    def _format_applicant_availability(
        availability: str,
        applicant_windows: List[Dict[str, Any]],
    ) -> str:
        if applicant_windows:
            return _format_structured_availability_display(applicant_windows) or "Not provided"
        if availability:
            return _format_availability_display(availability) or "Not provided"
        return "Not provided"

    @staticmethod
    def _format_applicant_availability_examples(
        availability: str,
        applicant_windows: List[Dict[str, Any]],
    ) -> str:
        if applicant_windows:
            return _format_structured_availability_examples(applicant_windows, limit=3)
        if availability:
            return _format_ticket_availability_display(availability)
        return ""

    def _build_overlap_details_text(
        self,
        case: Dict[str, Any],
        members: List[discord.Member],
        *,
        per_member_limit: int = 2,
        char_limit: int = 1000,
    ) -> str:
        if not members:
            return "No examiners match this request."
        blocks: List[str] = []
        for index, member in enumerate(self._unique_members(members)):
            overlap_text = self._format_overlap_windows(
                self._get_member_overlap_windows(case, member),
                limit=per_member_limit,
            )
            block = f"{member.mention}\n{overlap_text}"
            candidate = "\n\n".join(blocks + [block])
            if len(candidate) > char_limit:
                remaining = len(members) - index
                if remaining > 0:
                    if remaining == 1:
                        blocks.append("...and 1 more examiner")
                    else:
                        blocks.append(f"...and {remaining} more examiners")
                break
            blocks.append(block)
        return "\n\n".join(blocks) if blocks else "No examiners match this request."

    def _build_routing_embed(
        self,
        ticket_type: str,
        *,
        ticket_mention: str,
        opener_mention: str,
        th_level: Optional[int],
        from_clan: str,
        to_clan: str,
        elder_reason: str,
        elder_war: str,
        elder_clan: str,
        clan_codes: List[str],
        applicant_availability: Optional[str] = None,
        matched_examiners: Optional[str] = None,
        notes: Optional[str] = None,
        exam_required: bool = True,
    ) -> discord.Embed:
        title = "Clan Promotion Ticket" if ticket_type == "clan_promo" else "Elder Promotion Ticket"
        embed = discord.Embed(
            title=title,
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.add_field(name="Ticket", value=ticket_mention, inline=False)
        embed.add_field(name="Applicant", value=opener_mention, inline=False)
        if ticket_type == "clan_promo":
            embed.add_field(name="Town Hall", value=str(th_level) if th_level else "Not provided", inline=True)
            embed.add_field(
                name="Promotion",
                value=f"{from_clan} -> {to_clan}" if from_clan and to_clan else "Not provided",
                inline=False,
            )
            if exam_required:
                embed.add_field(name="Review Team", value="Examiners", inline=True)
                embed.add_field(name="Exam", value="Required", inline=True)
                embed.add_field(
                    name="Applicant Availability",
                    value=applicant_availability or "Not provided",
                    inline=False,
                )
                embed.add_field(
                    name="Matched Examiners",
                    value=matched_examiners or "No matching examiners",
                    inline=False,
                )
            else:
                embed.add_field(name="Review Team", value="Leadership", inline=True)
                embed.add_field(name="Exam", value="Not required", inline=True)
        else:
            embed.add_field(name="Reason", value=elder_reason or "Not provided", inline=False)
            embed.add_field(name="War to Review", value=elder_war or "Not provided", inline=False)
            embed.add_field(
                name="Clan",
                value=elder_clan or ", ".join(sorted(clan_codes)) or "Not provided",
                inline=False,
            )
            if matched_examiners and matched_examiners != "No examiners match this request.":
                embed.add_field(
                    name="Additional Reviewers",
                    value=matched_examiners,
                    inline=False,
                )
            embed.add_field(name="Review Team", value="Leadership review", inline=False)
        if notes:
            embed.add_field(name="Notes", value=notes, inline=False)
        return embed

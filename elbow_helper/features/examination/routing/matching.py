"""Member and clan matching helpers for examination routing."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

import discord

from elbow_helper.configuration.clans import CLAN_LEADERSHIP_ROLE_IDS
from elbow_helper.configuration.clans import CLAN_MEMBER_ROLE_IDS
from elbow_helper.configuration.clans import CLAN_ORDER
from elbow_helper.configuration.roles import LEAD

from ..availability import _all_overlap_windows_structured
from ..availability import _next_overlap_window
from .fields import extract_clan_codes

class ExaminationRoutingMatchingMixin:
    @staticmethod
    def _unique_members(members: List[discord.Member]) -> List[discord.Member]:
        return list({member.id: member for member in members}.values())

    def _resolve_leadership_role_ids(self, clan_codes: List[str]) -> List[int]:
        role_ids: List[int] = []
        seen: set[int] = set()
        for code in clan_codes:
            role_id = CLAN_LEADERSHIP_ROLE_IDS.get(code)
            if role_id and role_id not in seen:
                seen.add(role_id)
                role_ids.append(role_id)
        if role_ids:
            return role_ids
        return sorted(LEAD)

    @staticmethod
    def _is_leadership_member(member: discord.Member) -> bool:
        member_role_ids = {role.id for role in member.roles}
        return bool(member_role_ids & set(LEAD)) or bool(
            member_role_ids & set(CLAN_LEADERSHIP_ROLE_IDS.values())
        )

    def _get_case_matched_members(
        self,
        case: Dict[str, Any],
        guild: Optional[discord.Guild],
    ) -> List[discord.Member]:
        if guild is None:
            return []
        members: List[discord.Member] = []
        for user_id in case.get("pinged_ids") or []:
            try:
                member = guild.get_member(int(user_id))
            except (TypeError, ValueError):
                member = None
            if member:
                members.append(member)
        return self._unique_members(members)

    def _get_member_overlap_windows(
        self,
        case: Dict[str, Any],
        member: discord.Member,
    ) -> List[tuple[datetime, datetime]]:
        roster = self._get_examiner_roster()
        profile = roster.get(str(member.id), {})
        examiner_availability = profile.get("availability") or ""
        if not examiner_availability:
            return []
        examiner_timezone = profile.get("timezone") or "UTC"
        applicant_windows = case.get("availability_structured") or case.get("availability_windows") or []
        if applicant_windows:
            return _all_overlap_windows_structured(
                applicant_windows,
                examiner_availability,
                examiner_timezone,
            )
        availability = (case.get("availability") or "").strip()
        if not availability:
            return []
        overlap = _next_overlap_window(
            availability,
            examiner_availability,
            examiner_timezone,
        )
        return [overlap] if overlap else []

    def _resolve_clans(self, opener: Optional[discord.Member], text: str) -> List[str]:
        # Prefer explicit clan codes in text; fallback to opener roles.
        found = extract_clan_codes(text)
        if found:
            return found
        if opener:
            role_ids = {role.id for role in opener.roles}
            return [code for code in CLAN_ORDER if CLAN_MEMBER_ROLE_IDS.get(code) in role_ids]
        return []

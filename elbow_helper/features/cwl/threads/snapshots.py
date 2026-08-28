"""Thread feature CWL snapshot fetching and formatting."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from elbow_helper.domain.player_tags import encode_clash_tag
from ..helpers import coc_time_to_dt


class CwlThreadSnapshotMixin:
    async def _fetch_league_war(self, clan_name: str) -> Optional[Dict[str, Any]]:
        """Fetch the active CWL war (preparation/inWar) for this clan."""
        if not self.clash_client.configured:
            return None
        tag = self.clan_tags.get(clan_name)
        if not tag:
            return None
        group_path = f"/clans/{encode_clash_tag(tag)}/currentwar/leaguegroup"
        war_tags: List[str] = []
        group_response = await self.clash_client.get(
            group_path,
            attempts=1,
            timeout_seconds=15,
        )
        group = group_response.payload_object
        if group_response.status != 200 or group is None:
            return None
        for round_data in group.get("rounds", []) or []:
            for war_tag in round_data.get("warTags", []) or []:
                if war_tag and war_tag != "#0":
                    war_tags.append(war_tag)
        prep_candidate = None
        for war_tag in war_tags:
            war_response = await self.clash_client.get(
                f"/clanwarleagues/wars/{encode_clash_tag(war_tag)}",
                attempts=1,
                timeout_seconds=15,
            )
            war = war_response.payload_object
            if war_response.status != 200 or war is None:
                continue
            state = war.get("state")
            if state not in {"preparation", "inWar"}:
                continue
            clan_data = war.get("clan", {})
            opp_data = war.get("opponent", {})
            if clan_data.get("tag") != tag and opp_data.get("tag") != tag:
                continue
            war["_warTag"] = war_tag
            if state == "inWar":
                return war
            if prep_candidate is None:
                prep_candidate = war
        return prep_candidate


    async def _fetch_next_league_prep(self, clan_name: str) -> Optional[Dict[str, Any]]:
        """Fetch the next CWL war in preparation (if available)."""
        if not self.clash_client.configured:
            return None
        tag = self.clan_tags.get(clan_name)
        if not tag:
            return None
        group_response = await self.clash_client.get(
            f"/clans/{encode_clash_tag(tag)}/currentwar/leaguegroup",
            attempts=1,
            timeout_seconds=15,
        )
        group = group_response.payload_object
        if group_response.status != 200 or group is None:
            return None
        war_tags = []
        for round_data in group.get("rounds", []) or []:
            for war_tag in round_data.get("warTags", []) or []:
                if war_tag and war_tag != "#0":
                    war_tags.append(war_tag)
        for war_tag in war_tags:
            war_response = await self.clash_client.get(
                f"/clanwarleagues/wars/{encode_clash_tag(war_tag)}",
                attempts=1,
                timeout_seconds=15,
            )
            war = war_response.payload_object
            if war_response.status != 200 or war is None:
                continue
            if war.get("state") != "preparation":
                continue
            clan_data = war.get("clan", {})
            opp_data = war.get("opponent", {})
            if clan_data.get("tag") == tag or opp_data.get("tag") == tag:
                war["_warTag"] = war_tag
                return war
        return None


    async def _get_war_snapshot(self, clan_name: str) -> Optional[Dict[str, Any]]:
        # Only show during expected CWL window (1st-11th of the month) to avoid regular wars
        if not self._is_cwl_window():
            return None
        tag = self.clan_tags.get(clan_name)
        if not tag:
            return None

        # Use CWL league data only during the CWL window.
        data = await self._fetch_league_war(clan_name)
        if not data or data.get("state") in {"notInWar", "warEnded"}:
            return None
        state = data.get("state")
        start_dt = coc_time_to_dt(data.get("startTime"))
        # CWL round one can report "inWar" early; treat as prep if start time is in the future.
        if state == "inWar" and start_dt and start_dt > self._utc_now():
            state = "preparation"
        # Only show during prep or active war; hide after war ends or if not in war
        if state not in {"preparation", "inWar"}:
            return None

        clan = data.get("clan") or {}
        opponent = data.get("opponent") or {}
        if clan.get("tag") != tag and opponent.get("tag") == tag:
            clan = opponent
        elif clan.get("tag") != tag and opponent.get("tag") != tag:
            return None
        members = clan.get("members") or []
        per = data.get("attacksPerMember") or clan.get("attacksPerMember") or 1
        used = 0
        missing: List[str] = []

        time_key = "endTime" if state == "inWar" else "startTime"
        end_dt = coc_time_to_dt(data.get(time_key))
        time_left = "N/A"
        remaining_seconds: Optional[float] = None
        if end_dt:
            delta = end_dt - self._utc_now()
            if delta.total_seconds() < 0:
                delta = timedelta(seconds=0)
            remaining_seconds = delta.total_seconds()
            hours = int(remaining_seconds // 3600)
            minutes = int((remaining_seconds % 3600) // 60)
            time_left = f"{hours}h {minutes}m"

        show_missing = (
            state == "inWar"
            and remaining_seconds is not None
            and remaining_seconds <= 2 * 3600
        )

        if state == "inWar":
            for member in members:
                attacks = member.get("attacks") or []
                used += len(attacks)
                if show_missing and len(attacks) < per:
                    missing.append(member.get("name") or "Unknown")
        total = len(members) * per if members else 0

        next_prep = None
        next_prep_end_ts = None
        if state == "inWar":
            next_war = await self._fetch_next_league_prep(clan_name)
            if next_war:
                clan_data = next_war.get("clan", {})
                opp_data = next_war.get("opponent", {})
                if clan_data.get("tag") == tag:
                    next_prep = opp_data.get("name") or "Unknown"
                elif opp_data.get("tag") == tag:
                    next_prep = clan_data.get("name") or "Unknown"
                prep_end_dt = coc_time_to_dt(next_war.get("startTime"))
                if prep_end_dt:
                    next_prep_end_ts = int(prep_end_dt.timestamp())

        return {
            "state": state,
            "used": used,
            "total": total,
            "time_left": time_left,
            "missing": missing,
            "show_missing": show_missing,
            "next_prep": next_prep,
            "next_prep_end_ts": next_prep_end_ts,
        }

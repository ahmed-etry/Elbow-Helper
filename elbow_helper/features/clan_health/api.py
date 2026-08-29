
"""Clash API fetch and live collection helpers for clan health."""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from elbow_helper.configuration.clans import CLAN_NAMES, CLAN_ORDER, CLAN_TAGS
from elbow_helper.domain.cwl import is_cwl_window
from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.infrastructure.clash import ClashClient

from .analysis import ClanHealthAnalyzer
from .config import CWL_CLAN_CODES, UTC
from .database import ClanHealthRepository

LOGGER = logging.getLogger(__name__)


def _validated_clan_members(
    payload: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], Optional[int], bool]:
    raw_members = payload.get("memberList")
    declared_count = payload.get("members")
    if (
        not isinstance(raw_members, list)
        or isinstance(declared_count, bool)
        or not isinstance(declared_count, int)
        or declared_count < 0
    ):
        return [], None, False

    members = [
        member
        for member in raw_members
        if isinstance(member, dict) and str(member.get("tag") or "")
    ]
    member_tags = {
        str(member.get("tag") or "")
        for member in members
    }
    complete = (
        len(raw_members) == declared_count
        and len(members) == declared_count
        and len(member_tags) == declared_count
    )
    return members, declared_count, complete


class ClanHealthCollector:
    """Collect live Clash data and persist normalized Clan Health activity."""

    def __init__(
        self,
        clash_client: ClashClient,
        repository: ClanHealthRepository,
        analyzer: ClanHealthAnalyzer,
    ):
        self._clash_client = clash_client
        self.repository = repository
        self.analyzer = analyzer
        self._war_end_tasks: dict[str, asyncio.Task] = {}
        self._war_end_scheduled: dict[str, int] = {}
        self._cwl_war_end_tasks: dict[str, asyncio.Task] = {}
        self._cwl_war_end_scheduled: dict[str, int] = {}

    def close(self) -> None:
        for task in (
            *self._war_end_tasks.values(),
            *self._cwl_war_end_tasks.values(),
        ):
            if not task.done():
                task.cancel()
        self._war_end_tasks.clear()
        self._war_end_scheduled.clear()
        self._cwl_war_end_tasks.clear()
        self._cwl_war_end_scheduled.clear()

    async def _fetch_coc_json(
        self,
        path: str,
        *,
        retries: int = 4,
    ) -> Optional[Dict[str, Any]]:
        if not self._clash_client.configured:
            return None
        response = await self._clash_client.get(
            path,
            attempts=retries,
            timeout_seconds=20,
            backoff_seconds=1,
            maximum_backoff_seconds=30,
        )
        return response.payload_object if response.status == 200 else None

    async def _fetch_coc_json_with_status(
        self,
        path: str,
        *,
        retries: int = 4,
    ) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
        if not self._clash_client.configured:
            return None, None
        response = await self._clash_client.get(
            path,
            attempts=retries,
            timeout_seconds=20,
            backoff_seconds=1,
            maximum_backoff_seconds=30,
        )
        return response.status, response.payload_object

    @staticmethod
    def _is_expected_missing_cwl_league_group(
        status: Optional[int],
        payload: Optional[Dict[str, Any]],
    ) -> bool:
        state = str((payload or {}).get("state") or "").strip()
        if state == "notInWar":
            return True
        reason = str((payload or {}).get("reason") or "").strip()
        message = str((payload or {}).get("message") or "").strip().lower()
        if status == 404 and reason == "notFound":
            return True
        return status == 404 and "not in war" in message

    @staticmethod
    def _coc_time_to_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        for fmt in ("%Y%m%dT%H%M%S.%fZ", "%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
        return None

    @staticmethod
    def _war_blocks_for_clan(
        war_payload: Dict[str, Any],
        clan_tag: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        own = war_payload.get("clan", {}) or {}
        opponent = war_payload.get("opponent", {}) or {}
        if own.get("tag") != clan_tag and opponent.get("tag") == clan_tag:
            own, opponent = opponent, own
        if own.get("tag") != clan_tag:
            return {}, {}
        return own, opponent

    @staticmethod
    def _normalized_war_member_positions(members: List[Dict[str, Any]]) -> Dict[str, int]:
        # CWL war payloads can retain league-roster map positions; exports use the daily lineup order.
        tagged_members = [
            member
            for member in members
            if str(member.get("tag") or "")
        ]
        tagged_members.sort(
            key=lambda member: int(member.get("mapPosition") or 0)
        )
        return {
            str(member.get("tag")): position
            for position, member in enumerate(tagged_members, start=1)
        }

    def _extract_war_row(
        self,
        *,
        war_payload: Dict[str, Any],
        clan_code: str,
        clan_tag: str,
        war_id: str,
        war_type: str,
        source: str,
        cwl_season: str = "",
        cwl_league: str = "",
        cwl_round: int = 0,
    ) -> Optional[Dict[str, Any]]:
        war_state = str(war_payload.get("state") or "").strip()
        if war_state not in {"preparation", "inWar", "warEnded"}:
            return None
        own, opponent = self._war_blocks_for_clan(war_payload, clan_tag)
        if not own:
            return None

        preparation_start = self._coc_time_to_dt(war_payload.get("preparationStartTime"))
        start = self._coc_time_to_dt(war_payload.get("startTime"))
        end = self._coc_time_to_dt(war_payload.get("endTime"))
        members = own.get("members", []) or []
        team_size = int(war_payload.get("teamSize") or len(members))
        return {
            "war_id": war_id,
            "war_type": war_type,
            "clan_code": clan_code,
            "clan_tag": clan_tag,
            "opponent_tag": str(opponent.get("tag") or ""),
            "opponent_name": str(opponent.get("name") or ""),
            "cwl_season": cwl_season if war_type == "CWL" else "",
            "cwl_league": cwl_league if war_type == "CWL" else "",
            "cwl_round": int(cwl_round or 0) if war_type == "CWL" else 0,
            "team_size": team_size,
            "attacks_per_member": int(war_payload.get("attacksPerMember") or 1),
            "state": war_state,
            "preparation_start_ts": int(preparation_start.timestamp()) if preparation_start else 0,
            "start_ts": int(start.timestamp()) if start else 0,
            "end_ts": int(end.timestamp()) if end else 0,
            "last_seen_ts": int(time.time()),
            "source": source,
        }

    def _extract_final_war_roster_rows(
        self,
        *,
        war_payload: Dict[str, Any],
        clan_code: str,
        clan_tag: str,
        war_id: str,
        source: str,
    ) -> List[Dict[str, Any]]:
        war_state = str(war_payload.get("state") or "").strip()
        if war_state not in {"inWar", "warEnded"}:
            return []
        own, _ = self._war_blocks_for_clan(war_payload, clan_tag)
        if not own:
            return []

        captured_ts = int(time.time())
        attacks_per_member = int(war_payload.get("attacksPerMember") or 1)
        members = own.get("members", []) or []
        normalized_positions = self._normalized_war_member_positions(members)
        rows: List[Dict[str, Any]] = []
        for member in members:
            player_tag = str(member.get("tag") or "")
            if not player_tag:
                continue
            rows.append(
                {
                    "war_id": war_id,
                    "clan_code": clan_code,
                    "player_tag": player_tag,
                    "player_name": str(member.get("name") or player_tag),
                    "townhall": int(member.get("townhallLevel") or 0),
                    "map_position": int(normalized_positions.get(player_tag) or 0),
                    "attacks_expected": attacks_per_member,
                    "attacks_used": len(member.get("attacks", []) or []),
                    "roster_state": war_state,
                    "captured_ts": captured_ts,
                    "source": source,
                }
            )
        return rows

    async def _fetch_clan_war_league_name(
        self,
        clan_tag: str,
    ) -> str:
        now_ts = int(time.time())
        cache = getattr(self, "_clan_health_war_league_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, "_clan_health_war_league_cache", cache)
        cached = cache.get(clan_tag)
        if isinstance(cached, dict) and (now_ts - int(cached.get("fetched_at") or 0)) <= 21600:
            return str(cached.get("league_name") or "")

        path = f"/clans/{encode_clash_tag(clan_tag)}"
        payload = await self._fetch_coc_json(path, retries=3)
        league_name = str(((payload or {}).get("warLeague") or {}).get("name") or "").strip()
        cache[clan_tag] = {"league_name": league_name, "fetched_at": now_ts}
        return league_name

    def _extract_war_attack_rows(
        self,
        *,
        war_payload: Dict[str, Any],
        clan_code: str,
        clan_tag: str,
        war_id: str,
        war_type: str,
        source: str,
    ) -> List[Dict[str, Any]]:
        war_state = str(war_payload.get("state") or "").strip()
        if war_state not in {"preparation", "inWar", "warEnded"}:
            return []
        end_dt = self._coc_time_to_dt(war_payload.get("endTime"))
        if not end_dt:
            return []
        end_ts = int(end_dt.timestamp())
        # Ongoing wars are timestamped "now" so they participate in active-cycle windows.
        if war_state in {"preparation", "inWar"}:
            end_ts = int(time.time())

        own = war_payload.get("clan", {}) or {}
        opp = war_payload.get("opponent", {}) or {}
        # Normalize perspective so "own" always points to the tracked clan.
        if own.get("tag") != clan_tag and opp.get("tag") == clan_tag:
            own, opp = opp, own
        if own.get("tag") != clan_tag:
            return []

        opponent_members = opp.get("members", []) or []
        opponent_positions = self._normalized_war_member_positions(opponent_members)
        opponent_lookup: Dict[str, Dict[str, Any]] = {}
        for member in opponent_members:
            defender_tag = str(member.get("tag") or "")
            if defender_tag:
                opponent_lookup[defender_tag] = member

        out: List[Dict[str, Any]] = []
        for member in own.get("members", []) or []:
            player_tag = str(member.get("tag") or "")
            if not player_tag:
                continue
            player_name = str(member.get("name") or player_tag)
            attacks = member.get("attacks", []) or []
            for attack in attacks:
                defender_tag = str(attack.get("defenderTag") or "")
                defender = opponent_lookup.get(defender_tag, {})
                # Freshness is not a raw API field; it is the first attack order
                # seen against a defender in this war.
                attack_order = int(attack.get("order") or 0)
                if attack_order <= 0:
                    attack_order = len(out) + 1
                out.append(
                    {
                        "war_id": war_id,
                        "war_type": war_type,
                        "clan_code": clan_code,
                        "clan_tag": clan_tag,
                        "end_ts": end_ts,
                        "war_state": war_state,
                        "player_tag": player_tag,
                        "player_name": player_name,
                        "attack_order": attack_order,
                        "defender_tag": defender_tag,
                        "defender_name": str(defender.get("name") or defender_tag or "Unknown"),
                        "defender_map_position": int(opponent_positions.get(defender_tag) or 0),
                        "defender_townhall": int(defender.get("townhallLevel") or 0),
                        "stars": int(attack.get("stars") or 0),
                        "destruction": float(attack.get("destructionPercentage") or 0.0),
                        "fresh_attack": False,
                        "duration": int(attack.get("duration") or 0),
                        "source": source,
                    }
                )
        first_attack_order_by_defender: Dict[str, int] = {}
        for row in out:
            defender_tag = str(row.get("defender_tag") or "")
            if not defender_tag:
                continue
            attack_order = int(row.get("attack_order") or 0)
            previous = first_attack_order_by_defender.get(defender_tag)
            if previous is None or attack_order < previous:
                first_attack_order_by_defender[defender_tag] = attack_order
        for row in out:
            defender_tag = str(row.get("defender_tag") or "")
            row["fresh_attack"] = bool(
                defender_tag
                and int(row.get("attack_order") or 0) == first_attack_order_by_defender.get(defender_tag)
            )
        return out

    def _extract_war_member_rows(
        self,
        *,
        war_payload: Dict[str, Any],
        clan_code: str,
        clan_tag: str,
        war_id: str,
        war_type: str,
        source: str,
    ) -> List[Dict[str, Any]]:
        war_state = str(war_payload.get("state") or "").strip()
        if war_state not in {"preparation", "inWar", "warEnded"}:
            return []
        end_dt = self._coc_time_to_dt(war_payload.get("endTime"))
        if not end_dt:
            return []
        end_ts = int(end_dt.timestamp())
        # For ongoing/prep wars, include participation in current reports now
        # instead of waiting for warEnded.
        if war_state in {"preparation", "inWar"}:
            end_ts = int(time.time())
        own = war_payload.get("clan", {}) or {}
        opp = war_payload.get("opponent", {}) or {}
        if own.get("tag") != clan_tag and opp.get("tag") == clan_tag:
            own, opp = opp, own
        if own.get("tag") != clan_tag:
            return []

        attacks_per_member = int(war_payload.get("attacksPerMember") or 1)
        out: List[Dict[str, Any]] = []
        for member in own.get("members", []) or []:
            player_tag = str(member.get("tag") or "")
            if not player_tag:
                continue
            attacks = member.get("attacks", []) or []
            used = len(attacks)
            stars = 0.0
            destruction = 0.0
            for atk in attacks:
                stars += float(int(atk.get("stars") or 0))
                destruction += float(atk.get("destructionPercentage") or 0.0)
            out.append(
                {
                    "war_id": war_id,
                    "war_type": war_type,
                    "clan_code": clan_code,
                    "clan_tag": clan_tag,
                    "end_ts": end_ts,
                    "player_tag": player_tag,
                    "player_name": str(member.get("name") or player_tag),
                    "attacks_expected": attacks_per_member,
                    "attacks_used": used,
                    "stars": stars,
                    "destruction": destruction,
                    "attack_count": used,
                    "source": source,
                }
            )
        return out

    async def _fetch_cwl_wars_for_clan(
        self,
        *,
        clan_code: str,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        if clan_code not in CWL_CLAN_CODES:
            return [], []
        clan_tag = CLAN_TAGS[clan_code]
        warnings: List[str] = []
        if not is_cwl_window():
            return [], warnings

        group_path = f"/clans/{encode_clash_tag(clan_tag)}/currentwar/leaguegroup"
        group_status, group = await self._fetch_coc_json_with_status(group_path)
        war_refs: List[Tuple[int, str]] = []
        round_index = 1
        group_state = str((group or {}).get("state") or "").strip()
        group_season = str((group or {}).get("season") or "").strip()
        if group_state == "notInWar":
            return [], warnings
        if group:
            for round_data in group.get("rounds", []) or []:
                for war_tag in round_data.get("warTags", []) or []:
                    if war_tag and war_tag != "#0":
                        war_refs.append((round_index, str(war_tag)))
                round_index += 1
        if not war_refs:
            if not group and not self._is_expected_missing_cwl_league_group(group_status, group):
                warnings.append(f"{clan_code}: CWL league group unavailable")
            return [], warnings

        sem = asyncio.Semaphore(6)
        failed_war_tags: List[str] = []

        async def load_war(ref: Tuple[int, str]) -> Optional[Dict[str, Any]]:
            round_no, war_tag = ref
            path = f"/clanwarleagues/wars/{encode_clash_tag(war_tag)}"
            async with sem:
                war = await self._fetch_coc_json(path)
            if not war:
                failed_war_tags.append(war_tag)
                return None
            if war.get("clan", {}).get("tag") != clan_tag and war.get("opponent", {}).get("tag") != clan_tag:
                return None
            war["_round"] = round_no
            war["_warTag"] = war_tag
            war["_season"] = group_season
            return war

        payloads = [w for w in await asyncio.gather(*(load_war(ref) for ref in war_refs)) if w]
        for war in payloads:
            state = str(war.get("state") or "")
            war_tag = str(war.get("_warTag") or "")
            if state in {"preparation", "inWar"} and war_tag:
                end_dt = self._coc_time_to_dt(war.get("endTime"))
                if end_dt:
                    self._schedule_cwl_war_end_capture(war_tag, clan_code, int(end_dt.timestamp()))
        if failed_war_tags:
            warnings.append(
                f"{clan_code}: CWL war payload unavailable for {len(set(failed_war_tags))}/{len(war_refs)} wars"
            )
        return payloads, warnings

    async def _ingest_war_cwl_for_clan(
        self,
        *,
        clan_code: str,
    ) -> Tuple[int, int, List[str]]:
        clan_tag = CLAN_TAGS[clan_code]
        payloads, warnings = await self._fetch_cwl_wars_for_clan(clan_code=clan_code)
        if not payloads:
            return 0, 0, warnings
        league_name = await self._fetch_clan_war_league_name(clan_tag)
        war_rows: List[Dict[str, Any]] = []
        roster_rows: List[Dict[str, Any]] = []
        rows: List[Dict[str, Any]] = []
        attack_rows: List[Dict[str, Any]] = []
        war_ids: Set[str] = set()
        for war_payload in payloads:
            war_tag = str(war_payload.get("_warTag") or war_payload.get("warTag") or "")
            war_id = f"CWL:{war_tag}" if war_tag else ""
            if not war_id:
                continue
            source = "war_cwl_api"
            war_row = self._extract_war_row(
                war_payload=war_payload,
                clan_code=clan_code,
                clan_tag=clan_tag,
                war_id=war_id,
                war_type="CWL",
                source=source,
                cwl_season=str(war_payload.get("_season") or ""),
                cwl_league=league_name,
                cwl_round=int(war_payload.get("_round") or 0),
            )
            if war_row:
                war_rows.append(war_row)
            roster_rows.extend(
                self._extract_final_war_roster_rows(
                    war_payload=war_payload,
                    clan_code=clan_code,
                    clan_tag=clan_tag,
                    war_id=war_id,
                    source=source,
                )
            )
            member_rows = self._extract_war_member_rows(
                war_payload=war_payload,
                clan_code=clan_code,
                clan_tag=clan_tag,
                war_id=war_id,
                war_type="CWL",
                source=source,
            )
            if member_rows:
                rows.extend(member_rows)
                war_ids.add(war_id)
            attack_rows.extend(
                self._extract_war_attack_rows(
                    war_payload=war_payload,
                    clan_code=clan_code,
                    clan_tag=clan_tag,
                    war_id=war_id,
                    war_type="CWL",
                    source=source,
                )
            )
        stored_wars = await asyncio.to_thread(self.repository.store_wars, war_rows)
        stored_rosters = await asyncio.to_thread(self.repository.store_final_war_rosters, roster_rows)
        stored = await asyncio.to_thread(self.repository.store_war_activity, rows)
        stored_attacks = await asyncio.to_thread(self.repository.store_war_attacks, attack_rows)
        return len(war_ids), stored_wars + stored_rosters + stored + stored_attacks, warnings

    async def _ingest_regular_war_for_clan(
        self,
        *,
        clan_code: str,
    ) -> Tuple[int, int, List[str]]:
        clan_tag = CLAN_TAGS[clan_code]
        warnings: List[str] = []
        current_path = f"/clans/{encode_clash_tag(clan_tag)}/currentwar"
        war_payload = await self._fetch_coc_json(current_path)
        if not war_payload:
            return 0, 0, warnings
        war_state = str(war_payload.get("state") or "").strip()
        if war_state not in {"preparation", "inWar", "warEnded"}:
            return 0, 0, warnings
        end_time = str(war_payload.get("endTime") or "")
        war_tag = str(war_payload.get("tag") or "")
        opp = war_payload.get("opponent", {}) or {}
        opp_tag = str(opp.get("tag") or "")
        war_id = f"REG:{war_tag or f'{clan_tag}:{end_time}:{opp_tag}'}"
        source = f"currentwar_api:{war_state}"
        war_row = self._extract_war_row(
            war_payload=war_payload,
            clan_code=clan_code,
            clan_tag=clan_tag,
            war_id=war_id,
            war_type="REG",
            source=source,
        )
        roster_rows = self._extract_final_war_roster_rows(
            war_payload=war_payload,
            clan_code=clan_code,
            clan_tag=clan_tag,
            war_id=war_id,
            source=source,
        )
        rows = self._extract_war_member_rows(
            war_payload=war_payload,
            clan_code=clan_code,
            clan_tag=clan_tag,
            war_id=war_id,
            war_type="REG",
            source=source,
        )
        attack_rows = self._extract_war_attack_rows(
            war_payload=war_payload,
            clan_code=clan_code,
            clan_tag=clan_tag,
            war_id=war_id,
            war_type="REG",
            source=source,
        )
        stored_wars = await asyncio.to_thread(self.repository.store_wars, [war_row] if war_row else [])
        stored_rosters = await asyncio.to_thread(self.repository.store_final_war_rosters, roster_rows)
        stored = await asyncio.to_thread(self.repository.store_war_activity, rows)
        stored_attacks = await asyncio.to_thread(self.repository.store_war_attacks, attack_rows)
        if war_state in {"preparation", "inWar"}:
            end_dt = self._coc_time_to_dt(war_payload.get("endTime"))
            if end_dt:
                self._schedule_war_end_capture(clan_code, int(end_dt.timestamp()))
        return (1 if rows else 0), stored_wars + stored_rosters + stored + stored_attacks, warnings

    def _schedule_war_end_capture(self, clan_code: str, end_ts: int) -> None:
        # Idempotent: same endTime + live task → skip; new/changed endTime → cancel prior and reschedule.
        existing_task = self._war_end_tasks.get(clan_code)
        existing_end = self._war_end_scheduled.get(clan_code)
        if existing_end == end_ts and existing_task and not existing_task.done():
            return
        if existing_task and not existing_task.done():
            existing_task.cancel()
        self._war_end_scheduled[clan_code] = end_ts
        self._war_end_tasks[clan_code] = asyncio.create_task(
            self._run_war_end_capture(clan_code=clan_code, expected_end_ts=end_ts)
        )

    def _schedule_cwl_war_end_capture(self, war_tag: str, clan_code: str, end_ts: int) -> None:
        existing_task = self._cwl_war_end_tasks.get(war_tag)
        existing_end = self._cwl_war_end_scheduled.get(war_tag)
        if existing_end == end_ts and existing_task and not existing_task.done():
            return
        if existing_task and not existing_task.done():
            existing_task.cancel()
        self._cwl_war_end_scheduled[war_tag] = end_ts
        self._cwl_war_end_tasks[war_tag] = asyncio.create_task(
            self._run_cwl_war_end_capture(
                war_tag=war_tag,
                clan_code=clan_code,
                expected_end_ts=end_ts,
            )
        )

    async def _run_war_end_capture(self, *, clan_code: str, expected_end_ts: int) -> None:
        clan_tag = CLAN_TAGS[clan_code]
        try:
            delay = (expected_end_ts + 90) - int(time.time())
            if delay > 0:
                await asyncio.sleep(delay)
            if self._clash_client.configured:
                path = f"/clans/{encode_clash_tag(clan_tag)}/currentwar"
                for attempt in range(2):
                    payload = await self._fetch_coc_json(path)
                    if not payload:
                        return
                    state = str(payload.get("state") or "").strip()
                    end_dt = self._coc_time_to_dt(payload.get("endTime"))
                    payload_end_ts = int(end_dt.timestamp()) if end_dt else 0
                    if state == "warEnded":
                        war_tag = str(payload.get("tag") or "")
                        end_time_str = str(payload.get("endTime") or "")
                        opp_tag = str((payload.get("opponent") or {}).get("tag") or "")
                        war_id = f"REG:{war_tag or f'{clan_tag}:{end_time_str}:{opp_tag}'}"
                        source = "currentwar_api:warEnded_scheduled"
                        war_row = self._extract_war_row(
                            war_payload=payload,
                            clan_code=clan_code,
                            clan_tag=clan_tag,
                            war_id=war_id,
                            war_type="REG",
                            source=source,
                        )
                        roster_rows = self._extract_final_war_roster_rows(
                            war_payload=payload,
                            clan_code=clan_code,
                            clan_tag=clan_tag,
                            war_id=war_id,
                            source=source,
                        )
                        rows = self._extract_war_member_rows(
                            war_payload=payload,
                            clan_code=clan_code,
                            clan_tag=clan_tag,
                            war_id=war_id,
                            war_type="REG",
                            source=source,
                        )
                        attack_rows = self._extract_war_attack_rows(
                            war_payload=payload,
                            clan_code=clan_code,
                            clan_tag=clan_tag,
                            war_id=war_id,
                            war_type="REG",
                            source=source,
                        )
                        await asyncio.to_thread(self.repository.store_wars, [war_row] if war_row else [])
                        await asyncio.to_thread(self.repository.store_final_war_rosters, roster_rows)
                        await asyncio.to_thread(self.repository.store_war_activity, rows)
                        await asyncio.to_thread(self.repository.store_war_attacks, attack_rows)
                        return
                    # Rolled over into a new war — abandon; the 20-min loop will reschedule.
                    if payload_end_ts and payload_end_ts != expected_end_ts:
                        return
                    # Clock skew: API still says inWar at endTime+90s. Retry once at +3min.
                    if state == "inWar" and attempt == 0:
                        await asyncio.sleep(180)
                        continue
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("War-end capture failed clan=%s", clan_code)
        finally:
            if self._war_end_scheduled.get(clan_code) == expected_end_ts:
                self._war_end_scheduled.pop(clan_code, None)
            if self._war_end_tasks.get(clan_code) is asyncio.current_task():
                self._war_end_tasks.pop(clan_code, None)

    async def _run_cwl_war_end_capture(self, *, war_tag: str, clan_code: str, expected_end_ts: int) -> None:
        clan_tag = CLAN_TAGS[clan_code]
        try:
            delay = (expected_end_ts + 90) - int(time.time())
            if delay > 0:
                await asyncio.sleep(delay)
            if self._clash_client.configured:
                path = f"/clanwarleagues/wars/{encode_clash_tag(war_tag)}"
                for attempt in range(2):
                    payload = await self._fetch_coc_json(path)
                    if not payload:
                        return
                    payload["_warTag"] = war_tag
                    state = str(payload.get("state") or "").strip()
                    end_dt = self._coc_time_to_dt(payload.get("endTime"))
                    payload_end_ts = int(end_dt.timestamp()) if end_dt else 0
                    if payload_end_ts and payload_end_ts != expected_end_ts:
                        return
                    if state == "warEnded":
                        war_id = f"CWL:{war_tag}"
                        source = "cwl_api:warEnded_scheduled"
                        league_name = await self._fetch_clan_war_league_name(clan_tag)
                        war_row = self._extract_war_row(
                            war_payload=payload,
                            clan_code=clan_code,
                            clan_tag=clan_tag,
                            war_id=war_id,
                            war_type="CWL",
                            source=source,
                            cwl_league=league_name,
                        )
                        roster_rows = self._extract_final_war_roster_rows(
                            war_payload=payload,
                            clan_code=clan_code,
                            clan_tag=clan_tag,
                            war_id=war_id,
                            source=source,
                        )
                        rows = self._extract_war_member_rows(
                            war_payload=payload,
                            clan_code=clan_code,
                            clan_tag=clan_tag,
                            war_id=war_id,
                            war_type="CWL",
                            source=source,
                        )
                        attack_rows = self._extract_war_attack_rows(
                            war_payload=payload,
                            clan_code=clan_code,
                            clan_tag=clan_tag,
                            war_id=war_id,
                            war_type="CWL",
                            source=source,
                        )
                        await asyncio.to_thread(self.repository.store_wars, [war_row] if war_row else [])
                        await asyncio.to_thread(self.repository.store_final_war_rosters, roster_rows)
                        await asyncio.to_thread(self.repository.store_war_activity, rows)
                        await asyncio.to_thread(self.repository.store_war_attacks, attack_rows)
                        return
                    if state == "inWar" and attempt == 0:
                        await asyncio.sleep(180)
                        continue
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("CWL war-end capture failed clan=%s war_tag=%s", clan_code, war_tag)
        finally:
            if self._cwl_war_end_scheduled.get(war_tag) == expected_end_ts:
                self._cwl_war_end_scheduled.pop(war_tag, None)
            if self._cwl_war_end_tasks.get(war_tag) is asyncio.current_task():
                self._cwl_war_end_tasks.pop(war_tag, None)

    async def ingest_family_war_activity(
        self,
    ) -> Tuple[int, int, List[str]]:
        total_wars = 0
        total_rows = 0
        warnings: List[str] = []
        for clan_code in CLAN_ORDER:
            # CWL and regular wars are ingested separately because API endpoints differ.
            if clan_code in CWL_CLAN_CODES:
                war_cwl_wars, war_cwl_rows, war_cwl_warnings = await self._ingest_war_cwl_for_clan(
                    clan_code=clan_code,
                )
            else:
                war_cwl_wars, war_cwl_rows, war_cwl_warnings = 0, 0, []
            reg_wars, reg_rows, reg_warnings = await self._ingest_regular_war_for_clan(
                clan_code=clan_code,
            )
            total_wars += war_cwl_wars + reg_wars
            total_rows += war_cwl_rows + reg_rows
            warnings.extend(war_cwl_warnings)
            warnings.extend(reg_warnings)
        return total_wars, total_rows, warnings

    async def try_live_player_row(
        self,
        *,
        player_tag: str,
        season_key: str,
        cycle_start: datetime,
        cycle_end: datetime,
    ) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        warnings: List[str] = []
        if self._clash_client.configured:
            player_path = f"/players/{encode_clash_tag(player_tag)}"
            payload = await self._fetch_coc_json(player_path)
            if not payload:
                warnings.append("Current player details couldn't be loaded.")
                return None, warnings
            clan_tag = str(((payload.get("clan") or {}).get("tag")) or "")
            if not clan_tag:
                warnings.append("The player isn't currently in a clan.")
                return None, warnings
            clan_code = next((code for code, tag in CLAN_TAGS.items() if tag == clan_tag), None)
            if not clan_code:
                warnings.append("The player isn't currently in a clan covered by these reports.")
                return None, warnings

            entry, entry_warnings = await self.collect_clan_live(
                clan_code=clan_code,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            )
            warnings.extend(entry_warnings)
            players = list(entry.get("players", []))
            await asyncio.to_thread(
                self.analyzer.apply_war_activity,
                players,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            )
            await asyncio.to_thread(
                self.analyzer.apply_raid_activity,
                players,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            )
            await asyncio.to_thread(
                self.analyzer.apply_donation_activity,
                players,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            )
            await asyncio.to_thread(
                self.analyzer.apply_progression_fallback,
                players,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            )
            await asyncio.to_thread(
                self.analyzer.apply_flags,
                players,
                cycle_start=cycle_start,
                cycle_end=cycle_end,
            )
            for row in players:
                if str(row.get("player_tag") or "").upper() == player_tag.upper():
                    row["season_key"] = season_key
                    return row, warnings
            warnings.append("The player was not on the clan roster for the selected period.")
            return None, warnings
        warnings.append("Current player details couldn't be loaded.")
        return None, warnings

    async def collect_clan_live(
        self,
        *,
        clan_code: str,
        cycle_start: datetime,
        cycle_end: datetime,
    ) -> Tuple[Dict[str, Any], List[str]]:
        warnings: List[str] = []
        clan_tag = CLAN_TAGS[clan_code]
        clan_name = CLAN_NAMES[clan_code]

        clan_path = f"/clans/{encode_clash_tag(clan_tag)}"
        clan_payload = await self._fetch_coc_json(clan_path)
        if not clan_payload:
            LOGGER.info("Collect failed clan=%s reason=clan_profile_unavailable", clan_code)
            return {
                "clan_code": clan_code,
                "clan_name": clan_name,
                "players": [],
                "roster_complete": False,
                "declared_member_count": None,
                "returned_member_count": 0,
            }, [f"{clan_code}: clan profile unavailable"]

        members, declared_member_count, roster_complete = (
            _validated_clan_members(clan_payload)
        )
        if not roster_complete:
            returned_member_count = len(members)
            expected = (
                str(declared_member_count)
                if declared_member_count is not None
                else "unknown"
            )
            warnings.append(
                f"{clan_code}: clan roster incomplete "
                f"({returned_member_count}/{expected})"
            )
            LOGGER.warning(
                "Clan roster payload incomplete clan=%s returned=%s declared=%s",
                clan_code,
                returned_member_count,
                declared_member_count,
            )

        # Seed rows from member list first; player endpoint fills deep stats next.
        rows: Dict[str, Dict[str, Any]] = {}
        for member in members:
            tag = str(member.get("tag") or "")
            if not tag:
                continue
            rows[tag] = {
                "clan_code": clan_code,
                "clan_name": clan_name,
                "player_tag": tag,
                "player_name": str(member.get("name") or tag),
                "trophies": int(member.get("trophies") or 0),
                "donations": int(member.get("donations") or 0),
                "donations_received": int(member.get("donationsReceived") or 0),
                "townhall": None,
                "hero_sum": None,
                "pet_sum": None,
                "equipment_sum": None,
                "troop_sum": None,
                "spell_sum": None,
                "war_stars": None,
                "attack_wins": None,
                "capital_contrib": None,
                "games_total": None,
                "war_hits_used": 0,
                "war_hits_expected": 0,
                "war_missed": 0,
                "war_stars_total": 0.0,
                "war_destruction_total": 0.0,
                "war_attack_count": 0,
                "raid_attacks": 0,
                "raid_expected": 0,
                "raid_loot": 0,
                "raid_expected_estimated": False,
                "hero_delta": None,
                "pet_delta": None,
                "equipment_delta": None,
                "troop_delta": None,
                "spell_delta": None,
                "capital_delta": None,
                "th_delta": None,
                "games_delta": None,
                "flags": [],
                "status": "Good",
                "note": "",
                "priority_score": 0,
            }

        sem = asyncio.Semaphore(8)

        async def load_player(tag: str) -> None:
            row = rows[tag]
            path = f"/players/{encode_clash_tag(tag)}"
            async with sem:
                payload = await self._fetch_coc_json(path, retries=3)
            if not payload:
                warnings.append(f"{clan_code}: player fetch failed {tag}")
                return
            row["player_name"] = str(payload.get("name") or row["player_name"])
            row["townhall"] = int(payload.get("townHallLevel") or 0)
            row["war_stars"] = int(payload.get("warStars") or 0)
            row["attack_wins"] = int(payload.get("attackWins") or 0)
            row["capital_contrib"] = int(payload.get("clanCapitalContributions") or 0)
            hero_sum = 0
            for hero in payload.get("heroes", []) or []:
                if hero.get("village") and hero.get("village") != "home":
                    continue
                if isinstance(hero.get("level"), int):
                    hero_sum += int(hero["level"])
            row["hero_sum"] = hero_sum
            pet_sum = 0
            for pet in payload.get("troops", []) or []:
                # Pets are represented as troops in API with village=home and "isTroop" true.
                # We identify pets by known pet home levels via api section when available.
                if str(pet.get("village") or "").lower() != "home":
                    continue
                # Pets include "superTroopIsActive" as False and are not spell/equipment entries.
                # Name-based fallback is safer for broad API versions.
                if str(pet.get("name") or "").strip().lower() in {
                    "lassi", "electro owl", "mighty yak", "unicorn", "frosty", "diggy", "poison lizard", "phoenix", "spirit fox", "angry jelly"
                }:
                    if isinstance(pet.get("level"), int):
                        pet_sum += int(pet["level"])
            row["pet_sum"] = pet_sum
            equipment_sum = 0
            for eq in payload.get("heroEquipment", []) or []:
                if isinstance(eq.get("level"), int):
                    equipment_sum += int(eq["level"])
            row["equipment_sum"] = equipment_sum
            troop_sum = 0
            for troop in payload.get("troops", []) or []:
                if str(troop.get("village") or "").lower() != "home":
                    continue
                name = str(troop.get("name") or "").strip().lower()
                if name in {
                    "lassi", "electro owl", "mighty yak", "unicorn", "frosty", "diggy", "poison lizard", "phoenix", "spirit fox", "angry jelly"
                }:
                    continue
                if isinstance(troop.get("level"), int):
                    troop_sum += int(troop["level"])
            row["troop_sum"] = troop_sum
            spell_sum = 0
            for spell in payload.get("spells", []) or []:
                if str(spell.get("village") or "").lower() not in {"", "home"}:
                    continue
                if isinstance(spell.get("level"), int):
                    spell_sum += int(spell["level"])
            row["spell_sum"] = spell_sum
            for ach in payload.get("achievements", []) or []:
                if str(ach.get("name") or "").strip().lower() == "games champion":
                    row["games_total"] = int(ach.get("value") or 0)
                    break

        # Fan out player fetches with bounded concurrency to avoid burst pressure.
        await asyncio.gather(*(load_player(tag) for tag in list(rows.keys())))

        # During CWL, live wars are folded into this row before canonical storage overlays it.
        wars, cwl_warnings = await self._fetch_cwl_wars_for_clan(
            clan_code=clan_code,
        )
        warnings.extend(cwl_warnings)
        ended_in_window = 0
        for war in wars:
            if (war.get("state") or "") != "warEnded":
                continue
            ended_in_window += 1
            own = war.get("clan", {}) or {}
            opp = war.get("opponent", {}) or {}
            if own.get("tag") != clan_tag and opp.get("tag") == clan_tag:
                own, opp = opp, own
            if own.get("tag") != clan_tag:
                continue
            per = int(war.get("attacksPerMember") or 1)
            for member in own.get("members", []) or []:
                tag = str(member.get("tag") or "")
                if not tag:
                    continue
                row = rows.get(tag)
                if row is None:
                    continue
                attacks = member.get("attacks", []) or []
                used = len(attacks)
                row["war_hits_expected"] += per
                row["war_hits_used"] += used
                row["war_missed"] += max(0, per - used)
                for attack in attacks:
                    row["war_stars_total"] += float(int(attack.get("stars") or 0))
                    row["war_destruction_total"] += float(attack.get("destructionPercentage") or 0.0)
                    row["war_attack_count"] += 1

        # Capital raid contribution across whatever the API returns (up to 12 weekends).
        raid_path = f"/clans/{encode_clash_tag(clan_tag)}/capitalraidseasons?limit=12"
        raid_payload = await self._fetch_coc_json(raid_path)
        raid_weekends_used = 0
        raid_member_rows: List[Dict[str, Any]] = []
        for weekend in ((raid_payload or {}).get("items") or []):
            end_dt = self._coc_time_to_dt(weekend.get("endTime"))
            if not end_dt:
                continue
            members_payload = weekend.get("members") or []
            # Skip items with no members payload — they are historical weekends the
            # API no longer serves, and there is nothing verified to store for them.
            if not members_payload:
                continue
            raid_weekends_used += 1
            weekend_end_ts = int(end_dt.timestamp())
            weekend_id = f"RAID:{clan_tag}:{str(weekend.get('endTime') or weekend.get('startTime') or weekend_end_ts)}"
            for member in members_payload:
                tag = str(member.get("tag") or "")
                if not tag:
                    continue
                attacks = int(member.get("attacks") or 0)
                attack_limit = int(member.get("attackLimit") or 0)
                bonus_attack_limit = int(member.get("bonusAttackLimit") or 0)
                expected = attack_limit + bonus_attack_limit
                loot = int(member.get("capitalResourcesLooted") or 0)
                raid_member_rows.append(
                    {
                        "weekend_id": weekend_id,
                        "clan_code": clan_code,
                        "clan_tag": clan_tag,
                        "end_ts": weekend_end_ts,
                        "player_tag": tag,
                        "player_name": str(member.get("name") or tag),
                        "attacks": attacks,
                        "attack_limit": attack_limit,
                        "bonus_attack_limit": bonus_attack_limit,
                        "attacks_expected": expected,
                        "loot": loot,
                        "source": "capitalraidseasons_api",
                    }
                )

        await asyncio.to_thread(self.repository.store_raid_activity, raid_member_rows)

        # Track members where expected raids include non-participant estimation rows.
        estimated_raid_expectations = sum(1 for r in rows.values() if bool(r.get("raid_expected_estimated")))

        def delta_or_none(current: Any, baseline_value: Any) -> Optional[int]:
            if current is None or baseline_value is None:
                return None
            return int(current) - int(baseline_value)

        # Progression deltas from stored baseline.
        cutoff_ts = int(cycle_start.timestamp())
        player_tags = {str(row.get("player_tag") or "") for row in rows.values() if str(row.get("player_tag") or "")}
        baseline_map = self.repository.latest_snapshots(cutoff_ts=cutoff_ts, player_tags=player_tags)
        for row in rows.values():
            tag = str(row.get("player_tag") or "")
            baseline = baseline_map.get(tag)
            if baseline:
                # Deltas are computed from latest snapshot at/before cycle start.
                row["th_delta"] = delta_or_none(row.get("townhall"), baseline["townhall"])
                row["hero_delta"] = delta_or_none(row.get("hero_sum"), baseline["hero_sum"])
                row["pet_delta"] = delta_or_none(row.get("pet_sum"), baseline["pet_sum"])
                row["equipment_delta"] = delta_or_none(row.get("equipment_sum"), baseline["equipment_sum"])
                row["troop_delta"] = delta_or_none(row.get("troop_sum"), baseline["troop_sum"])
                row["spell_delta"] = delta_or_none(row.get("spell_sum"), baseline["spell_sum"])
                row["capital_delta"] = delta_or_none(row.get("capital_contrib"), baseline["capital_contrib"])
                row["games_delta"] = delta_or_none(row.get("games_total"), baseline["games_total"])

        players = list(rows.values())
        await asyncio.to_thread(
            self.analyzer.apply_flags,
            players,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
        )
        return {
            "clan_code": clan_code,
            "clan_name": clan_name,
            "players": players,
            "roster_complete": roster_complete,
            "declared_member_count": declared_member_count,
            "returned_member_count": len(members),
            "metrics": {
                "war_cwl_wars_in_window": ended_in_window,
                "war_cwl_war_refs": len({str(w.get('_warTag') or w.get('warTag') or '') for w in wars if str(w.get('_warTag') or w.get('warTag') or '')}),
                "raid_weekends_in_window": raid_weekends_used,
                "raid_expected_estimated_members": estimated_raid_expectations,
            },
        }, warnings

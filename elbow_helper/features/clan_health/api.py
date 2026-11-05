
"""Clash API fetch and live collection helpers for clan health."""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple

from elbow_helper.configuration.clans import CLAN_NAMES, CLAN_TAGS
from elbow_helper.domain.player_tags import encode_clash_tag, normalize_player_tag
from elbow_helper.infrastructure.clash import ClashClient

from .analysis import ClanHealthAnalyzer
from .config import UTC
from .database import ClanHealthRepository

LOGGER = logging.getLogger(__name__)


def _unit_name_key(name: Any) -> str:
    return "".join(char for char in str(name or "").casefold() if char.isalnum())


# Supercell's Pet List, checked 2026-10-27:
# https://support.supercell.com/clash-of-clans/en/articles/about-heroes-pets-9.html
_PET_NAME_KEYS = frozenset(
    _unit_name_key(name)
    for name in (
        "L.A.S.S.I", "Electro Owl", "Mighty Yak", "Unicorn", "Frosty", "Diggy",
        "Poison Lizard", "Phoenix", "Spirit Fox", "Angry Jelly", "Sneezy", "Greedy Raven",
    )
)


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
        {**member, "tag": tag}
        for member in raw_members
        if isinstance(member, dict) and (tag := normalize_player_tag(member.get("tag")))
    ]
    member_tags = {member["tag"] for member in members}
    complete = (
        len(raw_members) == declared_count
        and len(members) == declared_count
        and len(member_tags) == declared_count
    )
    return members, declared_count, complete


class ClanHealthCollector:
    """Collect live Clash data and persist normalized Clan Health activity.

    Wars are not collected here: the family collectors check regular and
    league wars for the family record, which health reads.
    """

    def __init__(
        self,
        clash_client: ClashClient,
        repository: ClanHealthRepository,
        analyzer: ClanHealthAnalyzer,
    ):
        self._clash_client = clash_client
        self.repository = repository
        self.analyzer = analyzer

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
            player_tag = normalize_player_tag(player_tag)
            if player_tag is None:
                warnings.append("Current player details couldn't be loaded.")
                return None, warnings
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
                if row.get("player_tag") == player_tag:
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
            tag = member["tag"]
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
            troop_sum = 0
            # Raw API troops include pets without a pet type discriminator.
            for troop in payload.get("troops", []) or []:
                if str(troop.get("village") or "").lower() != "home":
                    continue
                if not isinstance(troop.get("level"), int):
                    continue
                if _unit_name_key(troop.get("name")) in _PET_NAME_KEYS:
                    pet_sum += int(troop["level"])
                else:
                    troop_sum += int(troop["level"])
            row["pet_sum"] = pet_sum
            row["troop_sum"] = troop_sum
            equipment_sum = 0
            for eq in payload.get("heroEquipment", []) or []:
                if isinstance(eq.get("level"), int):
                    equipment_sum += int(eq["level"])
            row["equipment_sum"] = equipment_sum
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
                tag = normalize_player_tag(member.get("tag"))
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
                "raid_weekends_in_window": raid_weekends_used,
                "raid_expected_estimated_members": estimated_raid_expectations,
            },
        }, warnings

"""DB-backed analysis for the CWL roster planner."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any
from typing import Iterable
from typing import Sequence

import discord

from elbow_helper.features.rosters.config import CWL_SIGNUP_ROSTER_ID
from elbow_helper.configuration.clans import CLAN_ORDER

from .models import AssProfile
from .models import AssSeasonMetric
from .models import MegaAssMetric
from .models import profile_for_league


def _competition_ranks(
    rows: Sequence[Any],
    *,
    score_getter: Any,
    rank_setter: Any,
) -> None:
    ordered = sorted(
        (row for row in rows if score_getter(row) is not None),
        key=lambda row: (
            -float(score_getter(row)),
            str(getattr(row, "player_name", "")).casefold(),
            str(getattr(row, "player_tag", "")),
        ),
    )
    total = len(ordered)
    previous_score: float | None = None
    rank = 0
    for index, row in enumerate(ordered, start=1):
        score = float(score_getter(row))
        if previous_score is None or not math.isclose(score, previous_score, abs_tol=1e-9):
            rank = index
            previous_score = score
        rank_setter(row, rank, total)


def build_ass_season_metrics(
    *,
    wars: Sequence[dict[str, Any]],
    roster: Sequence[dict[str, Any]],
    attacks: Sequence[dict[str, Any]],
    season_order: dict[str, int],
    profiles_by_clan: dict[str, AssProfile],
) -> list[AssSeasonMetric]:
    wars_by_key = {
        (str(war.get("war_id") or ""), str(war.get("clan_code") or "")): war
        for war in wars
    }
    roster_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for member in roster:
        roster_by_key[
            (str(member.get("war_id") or ""), str(member.get("clan_code") or ""))
        ].append(member)
    attacks_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for attack in attacks:
        attacks_by_key[
            (str(attack.get("war_id") or ""), str(attack.get("clan_code") or ""))
        ].append(attack)

    metrics: dict[tuple[str, str, str], AssSeasonMetric] = {}
    for war_key, war in wars_by_key.items():
        season = str(war.get("cwl_season") or "")
        clan_code = str(war.get("clan_code") or "")
        if not season or clan_code not in profiles_by_clan:
            continue
        profile = profiles_by_clan[clan_code]
        war_roster = roster_by_key.get(war_key, [])
        roster_lookup = {
            str(member.get("player_tag") or ""): member
            for member in war_roster
            if str(member.get("player_tag") or "")
        }
        team_size = int(war.get("team_size") or 0)
        if team_size <= 0:
            team_size = max(
                (int(member.get("map_position") or 0) for member in war_roster),
                default=15,
            )
        midpoint = (team_size + 1) / 2

        for member in war_roster:
            player_tag = str(member.get("player_tag") or "")
            if not player_tag:
                continue
            metric_key = (season, clan_code, player_tag)
            metric = metrics.get(metric_key)
            if metric is None:
                metric = AssSeasonMetric(
                    season=season,
                    season_order=int(season_order.get(season, 0)),
                    latest_end_ts=int(war.get("end_ts") or 0),
                    clan_code=clan_code,
                    league=str(war.get("cwl_league") or ""),
                    profile=profile,
                    player_tag=player_tag,
                    player_name=str(member.get("player_name") or player_tag),
                    townhall=int(member.get("townhall") or 0),
                )
                metrics[metric_key] = metric
            metric.wars += 1
            expected = int(member.get("attacks_expected") or 0)
            if expected <= 0:
                expected = max(1, int(war.get("attacks_per_member") or 1))
            metric.attacks_expected += expected
            metric.latest_end_ts = max(metric.latest_end_ts, int(war.get("end_ts") or 0))
            metric.player_name = str(member.get("player_name") or metric.player_name)
            metric.townhall = max(metric.townhall, int(member.get("townhall") or 0))
            if war.get("cwl_league"):
                metric.league = str(war["cwl_league"])

        for attack in attacks_by_key.get(war_key, []):
            player_tag = str(attack.get("player_tag") or "")
            member = roster_lookup.get(player_tag)
            metric = metrics.get((season, clan_code, player_tag))
            if member is None or metric is None:
                continue
            metric.attacks += 1
            metric.stars += int(attack.get("stars") or 0)
            metric.destruction_total += float(attack.get("destruction") or 0.0)

            defender_position = int(attack.get("defender_map_position") or 0)
            attacker_position = int(member.get("map_position") or 0)
            if defender_position > 0 and attacker_position > 0:
                metric.position_attacks += 1
                metric.target_position_total += defender_position
                metric.target_distance_total += defender_position - attacker_position
                metric.defensive_position_total += attacker_position
                metric.difficulty_total += (
                    midpoint - defender_position
                ) * profile.difficulty_weight

    result = list(metrics.values())
    for metric in result:
        if metric.attacks <= 0:
            continue
        metric.projected_stars = (metric.stars / metric.attacks) * 7.0
        metric.missed_stars = (
            ((metric.attacks * 3) - metric.stars) / metric.attacks
        ) * 7.0
        metric.missed_adjustment = metric.profile.missed_adjustment(metric.missed_stars)
        metric.average_destruction = metric.destruction_total / metric.attacks
        if metric.position_attacks > 0:
            metric.average_target_position = (
                metric.target_position_total / metric.position_attacks
            )
            metric.average_target_distance = (
                metric.target_distance_total / metric.position_attacks
            )
            metric.average_defensive_position = (
                metric.defensive_position_total / metric.position_attacks
            )
            metric.difficulty_adjustment = (
                metric.difficulty_total / metric.position_attacks
            )
        else:
            metric.difficulty_adjustment = 0.0
        metric.score = (
            metric.projected_stars
            + metric.missed_adjustment
            + metric.difficulty_adjustment
        ) * (metric.average_destruction / 100.0)

    grouped: dict[tuple[str, str], list[AssSeasonMetric]] = defaultdict(list)
    for metric in result:
        grouped[(metric.season, metric.clan_code)].append(metric)
    for group in grouped.values():
        _competition_ranks(
            group,
            score_getter=lambda item: item.score,
            rank_setter=lambda item, rank, total: (
                setattr(item, "rank", rank),
                setattr(item, "rank_total", total),
            ),
        )
    return result


def build_mega_ass_metrics(
    season_metrics: Sequence[AssSeasonMetric],
) -> list[MegaAssMetric]:
    grouped: dict[tuple[str, str], list[AssSeasonMetric]] = defaultdict(list)
    for metric in season_metrics:
        if metric.score is None:
            continue
        grouped[(metric.clan_code, metric.player_tag)].append(metric)

    mega_rows: list[MegaAssMetric] = []
    for (clan_code, player_tag), metrics in grouped.items():
        ordered = tuple(sorted(metrics, key=lambda item: item.season_order))
        total_attacks = sum(item.attacks for item in ordered)
        defensive_weight = sum(
            (item.average_defensive_position or 0.0) * item.attacks
            for item in ordered
            if item.average_defensive_position is not None
        )
        defensive_attacks = sum(
            item.attacks
            for item in ordered
            if item.average_defensive_position is not None
        )
        latest = ordered[-1]
        mega_rows.append(
            MegaAssMetric(
                clan_code=clan_code,
                profile=latest.profile,
                player_tag=player_tag,
                player_name=latest.player_name,
                townhall=max(item.townhall for item in ordered),
                seasons=ordered,
                score=sum(float(item.score) for item in ordered) / len(ordered),
                total_attacks=total_attacks,
                average_defensive_position=(
                    defensive_weight / defensive_attacks
                    if defensive_attacks > 0
                    else None
                ),
            )
        )

    mega_by_clan: dict[str, list[MegaAssMetric]] = defaultdict(list)
    for row in mega_rows:
        mega_by_clan[row.clan_code].append(row)
    for group in mega_by_clan.values():
        _competition_ranks(
            group,
            score_getter=lambda item: item.score,
            rank_setter=lambda item, rank, total: (
                setattr(item, "rank", rank),
                setattr(item, "rank_total", total),
            ),
        )
    return mega_rows


class CwlRosterAnalysisMixin:
    def _load_roster_history(
        self,
        history_limit: int | None,
    ) -> dict[str, Any]:
        return self.clan_health_repository.roster_history(history_limit)

    @staticmethod
    def _profiles_for_roster_history(
        wars: Sequence[dict[str, Any]],
    ) -> tuple[dict[str, AssProfile], dict[str, str]]:
        latest_by_clan: dict[str, dict[str, Any]] = {}
        for war in wars:
            clan_code = str(war.get("clan_code") or "")
            if not clan_code:
                continue
            current = latest_by_clan.get(clan_code)
            war_has_league = bool(str(war.get("cwl_league") or "").strip())
            current_has_league = bool(
                str((current or {}).get("cwl_league") or "").strip()
            )
            if current is None or (
                war_has_league
                and (
                    not current_has_league
                    or int(war.get("end_ts") or 0)
                    > int(current.get("end_ts") or 0)
                )
            ) or (
                not current_has_league
                and int(war.get("end_ts") or 0)
                > int(current.get("end_ts") or 0)
            ):
                latest_by_clan[clan_code] = war
        profiles = {
            clan_code: profile_for_league(str(war.get("cwl_league") or ""))
            for clan_code, war in latest_by_clan.items()
        }
        leagues = {
            clan_code: str(war.get("cwl_league") or "Unknown")
            for clan_code, war in latest_by_clan.items()
        }
        return profiles, leagues

    def _analyze_roster_history(
        self,
        history_limit: int | None,
    ) -> dict[str, Any]:
        dataset = self._load_roster_history(history_limit)
        seasons = list(dataset["seasons"])
        season_order = {
            str(row["key"]): index
            for index, row in enumerate(reversed(seasons), start=1)
        }
        profiles, latest_leagues = self._profiles_for_roster_history(dataset["wars"])
        season_metrics = build_ass_season_metrics(
            wars=dataset["wars"],
            roster=dataset["roster"],
            attacks=dataset["attacks"],
            season_order=season_order,
            profiles_by_clan=profiles,
        )
        mega_metrics = build_mega_ass_metrics(season_metrics)
        return {
            **dataset,
            "profiles": profiles,
            "latest_leagues": latest_leagues,
            "season_metrics": season_metrics,
            "mega_metrics": mega_metrics,
        }

    def _load_directory_players(
        self,
        player_tags: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        return self.clan_health_repository.directory_players(player_tags)

    def _load_active_roster_records(
        self,
        member_ids: Iterable[int],
    ) -> list[dict[str, Any]]:
        return self.record_reader.active_for_members(member_ids)

    async def _build_roster_candidates(
        self,
        *,
        guild: discord.Guild,
        season_metrics: Sequence[AssSeasonMetric],
        mega_metrics: Sequence[MegaAssMetric],
    ) -> dict[str, Any]:
        signup_roster = await self.roster_queries.get(CWL_SIGNUP_ROSTER_ID)
        if signup_roster is None:
            raise RuntimeError("The CWL signup roster hasn't been set up.")
        signup_rows = await self.roster_queries.members(signup_roster)
        signed_member_ids = {
            int(row.discord_user_id)
            for row in signup_rows
            if int(row.discord_user_id) > 0
        }
        if not signup_rows:
            return {
                "candidates": [],
                "signed_member_count": 0,
                "signed_account_count": 0,
                "signed_tags": set(),
                "records": [],
                "links_by_user": {},
            }

        latest_metric_by_tag: dict[str, AssSeasonMetric] = {}
        for metric in season_metrics:
            current = latest_metric_by_tag.get(metric.player_tag)
            if current is None or (
                metric.season_order,
                metric.latest_end_ts,
            ) > (
                current.season_order,
                current.latest_end_ts,
            ):
                latest_metric_by_tag[metric.player_tag] = metric
        mega_by_key = {
            (metric.clan_code, metric.player_tag): metric
            for metric in mega_metrics
        }

        links_by_user: dict[int, list[dict[str, Any]]] = {}
        links_by_tag: dict[str, dict[str, Any]] = {}
        for member_id in signed_member_ids:
            links = list(
                self.account_links.get_links_for_user(member_id)
            )
            links_by_user[member_id] = links
            for link in links:
                player_tag = str(link.get("player_tag") or "")
                if player_tag:
                    links_by_tag[player_tag] = link
        signed_tags = {
            str(row.player_tag)
            for row in signup_rows
            if str(row.player_tag)
        }
        all_tags = set(signed_tags)
        directory = self._load_directory_players(all_tags)
        live_locations = {
            player_tag: location
            for player_tag in all_tags
            if (
                location := self.account_links.get_player_location(
                    player_tag
                )
            )
        }

        records = self._load_active_roster_records(signed_member_ids)
        records_by_user: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            records_by_user[int(record.get("member_id") or 0)].append(record)

        clan_order = {code: index for index, code in enumerate(CLAN_ORDER)}
        candidates: list[dict[str, Any]] = []
        for row in signup_rows:
            member_id = int(row.discord_user_id)
            member = guild.get_member(member_id)
            player_tag = str(row.player_tag)
            link = links_by_tag.get(player_tag, {})
            latest = latest_metric_by_tag.get(player_tag)
            mega = (
                mega_by_key.get((latest.clan_code, player_tag))
                if latest is not None
                else None
            )
            directory_row = directory.get(player_tag, {})
            live_row = live_locations.get(player_tag, {})
            candidates.append(
                {
                    "discord_user_id": member_id,
                    "discord_member": (
                        member.display_name if member is not None else str(member_id)
                    ),
                    "account_name": str(
                        live_row.get("player_name")
                        or directory_row.get("player_name")
                        or row.player_name
                        or link.get("player_name_last_seen")
                        or (latest.player_name if latest else "")
                        or player_tag
                    ),
                    "player_tag": player_tag,
                    "townhall": int(
                        live_row.get("townhall")
                        or directory_row.get("townhall")
                        or row.townhall
                        or (latest.townhall if latest else 0)
                        or 0
                    ),
                    "current_clan": str(
                        live_row.get("clan_code")
                        or directory_row.get("clan_code")
                        or row.clan_code
                        or ""
                    ),
                    "latest": latest,
                    "mega": mega,
                    "cwl_records": sum(
                        1
                        for record in records_by_user.get(member_id, [])
                        if str(record.get("category_key") or "") == "cwl"
                    ),
                }
            )

        def candidate_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
            latest = row.get("latest")
            mega = row.get("mega")
            previous_clan = latest.clan_code if latest else ""
            return (
                clan_order.get(previous_clan, len(clan_order)),
                -int(row.get("townhall") or 0),
                int(mega.rank) if mega and mega.rank is not None else 10_000,
                int(latest.rank) if latest and latest.rank is not None else 10_000,
                str(row.get("account_name") or "").casefold(),
            )

        candidates.sort(key=candidate_sort_key)
        return {
            "candidates": candidates,
            "signed_member_count": len(signed_member_ids),
            "signed_account_count": len(signup_rows),
            "signed_tags": signed_tags,
            "records": records,
            "links_by_user": links_by_user,
        }

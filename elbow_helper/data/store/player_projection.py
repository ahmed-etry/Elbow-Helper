"""Typed profile projections; ambiguous troop/pet identities remain unknown."""

from collections import Counter
from dataclasses import dataclass

from elbow_helper.domain.player_tags import normalize_player_tag

from .projections import canonical_json, nonnegative_int


HOUR_US = 3_600_000_000
COUNTERS = {
    "trophies": "trophies", "bestTrophies": "best_trophies",
    "builderBaseTrophies": "builder_trophies", "bestBuilderBaseTrophies": "best_builder_trophies",
    "warStars": "war_stars", "attackWins": "attack_wins", "defenseWins": "defense_wins",
    "clanCapitalContributions": "capital_contributions", "donations": "donations",
    "donationsReceived": "received",
}
INTEGER_FIELDS = {
    "townHallLevel", "townHallWeaponLevel", "expLevel", "builderHallLevel",
    "currentLeagueSeasonId", "previousLeagueSeasonId",
}
TEXT_FIELDS = {"name", "role", "warPreference", "currentLeagueGroupTag", "previousLeagueGroupTag"}
OBJECT_FIELDS = {
    "clan", "league", "builderBaseLeague", "leagueTier", "playerHouse",
    "currentRankedGroup", "previousRankedGroup", "rankedGroup", "previousLeagueTier",
}
ARRAYS = {"troops": None, "pets": "pet", "heroes": "hero", "heroEquipment": "equipment",
          "spells": "spell", "achievements": "achievement"}
TRANSIENT = {"superTroopIsActive", "boosted", "boostTime", "boostExpirationTime",
             "superTroopCooldown", "originalLevel", "equipment"}
LEGEND_SLOW = {"bestSeason", "previousSeason", "bestBuilderBaseSeason", "previousBuilderBaseSeason"}
LEGEND_FAST = {"legendTrophies", "currentSeason", "currentDay", "previousDay"}


def _json_value(value):
    # JSON encoding also rejects nonfinite numbers. Booleans remain typed.
    if value is None or type(value) in (str, bool, int, float):
        canonical_json(value)
        return True
    if isinstance(value, list):
        return all(_json_value(item) for item in value)
    return isinstance(value, dict) and all(isinstance(key, str) and _json_value(item)
                                          for key, item in value.items())


@dataclass
class Profile:
    fields: dict
    items: dict
    complete_arrays: dict
    counters: dict
    legend: dict | None
    unsupported: list

    @property
    def complete(self):
        # Unprojected API fields are diagnostic, not evidence of missing data.
        return all(self.complete_arrays.values())


def project_profile(payload, tag):
    if (not isinstance(payload, dict) or normalize_player_tag(payload.get("tag")) != tag
            or not isinstance(payload.get("name"), str) or not payload["name"]
            or not nonnegative_int(payload.get("townHallLevel"))):
        raise ValueError("invalid_profile")
    known = {"tag", *COUNTERS, *INTEGER_FIELDS, *TEXT_FIELDS, *OBJECT_FIELDS, *ARRAYS,
             "labels", "legendStatistics"}
    unsupported = [key for key in payload if key not in known]
    fields = {}
    for key in INTEGER_FIELDS | TEXT_FIELDS | OBJECT_FIELDS | {"labels"}:
        if key not in payload:
            continue
        value = payload[key]
        valid = (nonnegative_int(value) if key in INTEGER_FIELDS else isinstance(value, str)
                 if key in TEXT_FIELDS else isinstance(value, list) if key == "labels" else isinstance(value, dict))
        if not valid or not _json_value(value):
            # A malformed known field must not look like a disappearance.
            raise ValueError("invalid_profile_field")
        if key in {"currentLeagueGroupTag", "previousLeagueGroupTag"}:
            value = normalize_player_tag(value)
            if value is None:
                raise ValueError("invalid_ranked_group_tag")
        fields[key] = value
    counters = {}
    for key in COUNTERS:
        if key in payload:
            if not nonnegative_int(payload[key]):
                raise ValueError("invalid_counter")
            counters[key] = payload[key]
    legend = None
    if "legendStatistics" in payload:
        raw = payload["legendStatistics"]
        if not isinstance(raw, dict) or not _json_value(raw):
            raise ValueError("invalid_legend")
        fields.update({f"legendStatistics.{key}": value for key, value in raw.items() if key in LEGEND_SLOW})
        legend = {key: value for key, value in raw.items() if key in LEGEND_FAST}
        unsupported.extend(f"legendStatistics.{key}" for key in raw if key not in LEGEND_SLOW | LEGEND_FAST)
    items, complete_arrays = {}, {}
    for source in ARRAYS:
        rows = payload.get(source)
        complete_arrays[source] = isinstance(rows, list)
        items[source] = []
        if not isinstance(rows, list):
            continue
        valid = []
        for row in rows:
            if not _valid_item(row, source):
                complete_arrays[source] = False
                continue
            valid.append(row)
        counts = Counter(item_identity(source, row) for row in valid)
        items[source] = [row for row in valid if counts[item_identity(source, row)] == 1]
        if len(items[source]) != len(rows):
            complete_arrays[source] = False
    # pets is optional in the raw API. Its absence supplies no removal evidence.
    if "pets" not in payload:
        complete_arrays.pop("pets")
    return Profile(fields, items, complete_arrays, counters, legend, unsupported)


def _valid_item(row, source):
    if (not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"]
            or not isinstance(row.get("village", ""), str)
            or not _json_value(row)):
        return False
    required = ("stars", "target") if source == "achievements" else ("level", "maxLevel")
    if any(not nonnegative_int(row.get(key)) for key in required):
        return False
    if any(key in row and not nonnegative_int(row[key]) for key in ("level", "maxLevel", "stars", "target", "value")):
        return False
    return "isPet" not in row or type(row["isPet"]) is bool


def item_identity(source, row):
    # Two official achievements share a name and village, but have different
    # instructions. Keep both instead of treating that real shape as a conflict.
    discriminator = row.get("info", "") if source == "achievements" else ""
    return row.get("village", ""), row["name"], discriminator if isinstance(discriminator, str) else ""

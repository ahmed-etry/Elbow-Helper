"""Conservative player-field projection; absent fields are never fabricated."""

from collections.abc import Mapping


PLAYER_COUNTERS = ("trophies", "bestTrophies", "attackWins", "defenseWins")
LEAGUE_FIELDS = ("id", "name")
SEASON_FIELDS = ("id", "rank", "trophies")


def project_player(payload, report_missing):
    result = {}

    def select(source, fields, prefix=""):
        selected = {}
        for field in fields:
            value = source.get(field)
            valid = (isinstance(value, str) and bool(value) if field in ("id", "name")
                     else type(value) is int and value >= 0)
            if field == "id" and type(value) is int and value >= 0:
                valid = True
            report_missing(prefix + field, not valid)
            if valid:
                selected[field] = value
        return selected

    result.update(select(payload, PLAYER_COUNTERS))
    for field in ("leagueTier", "league"):
        value = payload.get(field)
        valid = isinstance(value, Mapping)
        report_missing(field, not valid)
        if valid:
            result[field] = select(value, LEAGUE_FIELDS, field + ".")
    legend = payload.get("legendStatistics")
    report_missing("legendStatistics", not isinstance(legend, Mapping))
    if isinstance(legend, Mapping):
        selected = select(legend, ("legendTrophies",), "legendStatistics.")
        for field in ("currentSeason", "previousSeason", "bestSeason"):
            value = legend.get(field)
            report_missing("legendStatistics." + field, not isinstance(value, Mapping))
            if isinstance(value, Mapping):
                selected[field] = select(value, SEASON_FIELDS, "legendStatistics." + field + ".")
        result["legendStatistics"] = selected
    return result

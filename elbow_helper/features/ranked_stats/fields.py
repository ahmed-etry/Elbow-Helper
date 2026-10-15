"""Conservative player-field projection; absent fields are never fabricated."""

from collections.abc import Mapping


PLAYER_COUNTERS = ("trophies", "bestTrophies", "attackWins", "defenseWins")
LEAGUE_FIELDS = ("id", "name")
SEASON_FIELDS = ("id", "rank", "trophies")


def project_player(payload, report_missing):
    result = {}

    def select(source, fields, prefix="", optional=False):
        selected = {}
        for field in fields:
            value = source.get(field)
            valid = (isinstance(value, str) and bool(value) if field in ("id", "name")
                     else type(value) is int and value >= 0)
            if field == "id" and type(value) is int and value >= 0:
                valid = True
            # Optional fields are only reported when present but malformed.
            report_missing(prefix + field, not valid and not (optional and value is None))
            if valid:
                selected[field] = value
        return selected

    def section(source, field, prefix=""):
        # Players outside Legend League, or without a previous season, omit these sections.
        value = source.get(field)
        report_missing(prefix + field, value is not None and not isinstance(value, Mapping))
        return value if isinstance(value, Mapping) else None

    result.update(select(payload, PLAYER_COUNTERS))
    tier = payload.get("leagueTier")
    report_missing("leagueTier", not isinstance(tier, Mapping))
    if isinstance(tier, Mapping):
        result["leagueTier"] = select(tier, LEAGUE_FIELDS, "leagueTier.")
    league = section(payload, "league")
    if league is not None:
        result["league"] = select(league, LEAGUE_FIELDS, "league.")
    legend = section(payload, "legendStatistics")
    if legend is not None:
        selected = select(legend, ("legendTrophies",), "legendStatistics.", optional=True)
        for field in ("currentSeason", "previousSeason", "bestSeason"):
            value = section(legend, field, "legendStatistics.")
            if value is not None:
                selected[field] = select(value, SEASON_FIELDS, "legendStatistics." + field + ".", optional=True)
        result["legendStatistics"] = selected
    return result

"""Complete historical CWL performance snapshots with bounded pages."""

from dataclasses import asdict, dataclass, field
import json
from typing import Any

from elbow_helper.configuration.clans import CLAN_ORDER
from elbow_helper.features.cwl.queries import CwlPerformanceSnapshot
from elbow_helper.features.cwl.queries import MAX_HISTORY_SEASONS


@dataclass(frozen=True, slots=True)
class CwlPerformanceReport:
    report_id: str
    guild_id: int
    snapshot: CwlPerformanceSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        seasons = self.snapshot.seasons
        if type(self.guild_id) is not int or self.guild_id <= 0:
            raise ValueError("Invalid CWL report guild")
        if not isinstance(self.report_id, str) or not self.report_id or not isinstance(self.snapshot.observed_at, str) or not self.snapshot.observed_at:
            raise ValueError("Invalid CWL report identity")
        if type(self.snapshot.history_limit) is not int or not 1 <= self.snapshot.history_limit <= MAX_HISTORY_SEASONS:
            raise ValueError("Invalid CWL report history limit")
        if len(seasons) != len(set(seasons)) or any(not isinstance(season, str) or not season for season in seasons):
            raise ValueError("Invalid CWL report seasons")
        if any(row.season not in seasons or row.clan_code not in CLAN_ORDER
               for row in (*self.snapshot.clan_seasons, *self.snapshot.rows)):
            raise ValueError("CWL report rows do not belong to the snapshot")
        payload = {"report_id": self.report_id, "guild_id": self.guild_id,
                   "snapshot": asdict(self.snapshot)}
        object.__setattr__(self, "retained_bytes", len(json.dumps(
            payload, ensure_ascii=False,
        ).encode("utf-8")))

    def manifest(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "kind": "cwl_performance",
            "observed_at": self.snapshot.observed_at,
            "history_limit": self.snapshot.history_limit,
            "seasons": list(self.snapshot.seasons),
            "row_count": len(self.snapshot.rows),
        }

    def page(
        self, *, season: str | None = None, clan_code: str | None = None,
        player_tag: str | None = None, offset: int = 0, limit: int = 25,
    ) -> dict[str, Any]:
        rows = tuple(row for row in self.snapshot.rows if (
            (season is None or row.season == season)
            and (clan_code is None or row.clan_code == clan_code)
            and (player_tag is None or row.player_tag == player_tag)
        ))
        summaries = tuple(row for row in self.snapshot.clan_seasons if (
            (season is None or row.season == season)
            and (clan_code is None or row.clan_code == clan_code)
        ))
        return {
            **self.manifest(),
            "filters": {"season": season, "clan_code": clan_code, "player_tag": player_tag},
            "matching_rows": len(rows),
            "matching_accounts": len({row.player_tag for row in rows}),
            "attacks": sum(row.attacks for row in rows),
            "attacks_expected": sum(row.attacks_expected for row in rows),
            "clan_seasons": [asdict(row) for row in summaries],
            "players": [asdict(row) for row in rows[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "complete_snapshot": True,
        }

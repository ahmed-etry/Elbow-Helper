"""Retained clan-health reports and deterministic period comparisons."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
import json
import math
from typing import Any

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.features.clan_health.queries import (
    ClanHealthPlayerRow, ClanHealthReportSnapshot,
)


_COMPARISON_FIELDS = (
    "player_name", "status", "flags", "note", "war_hits_used",
    "war_hits_expected", "war_missed", "war_stars_total",
    "war_destruction_total", "war_attack_count", "raid_attacks",
    "raid_expected", "raid_expected_estimated", "raid_loot", "donations",
    "donations_received", "townhall", "hero_sum", "games_total", "hero_delta",
    "capital_delta", "th_delta", "games_delta",
)
_DELTA_FIELDS = tuple(
    field for field in _COMPARISON_FIELDS
    if field not in {"player_name", "status", "flags", "note", "raid_expected_estimated"}
)
_TOTAL_FIELDS = (
    "war_hits_used", "war_hits_expected", "war_missed", "war_stars_total",
    "war_destruction_total", "war_attack_count", "raid_attacks", "raid_expected", "raid_loot",
    "donations", "donations_received",
)


@dataclass(frozen=True, slots=True)
class ClanHealthReport:
    report_id: str
    guild_id: int
    snapshot: ClanHealthReportSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.report_id, str) or not self.report_id
            or len(self.report_id) > 32 or type(self.guild_id) is not int
            or self.guild_id <= 0 or self.snapshot.clan_code not in CLANS
        ):
            raise ValueError("Invalid clan-health report identity")
        run = self.snapshot.run
        if (
            not run.run_id or len(run.run_id) > 100 or not run.season_key
            or any(type(value) is not int for value in (
                run.created_ts, run.cycle_start_ts, run.cycle_end_ts, run.player_count,
            ))
            or run.created_ts <= 0 or run.cycle_start_ts <= 0
            or run.cycle_end_ts <= run.cycle_start_ts
            or run.player_count != len(self.snapshot.rows)
        ):
            raise ValueError("Invalid clan-health report period")
        tags = [row.player_tag for row in self.snapshot.rows]
        if len(tags) != len(set(tags)):
            raise ValueError("Duplicate account in clan-health report")
        for row in self.snapshot.rows:
            _validate_row(row, self.snapshot.clan_code)
        if (
            len(self.snapshot.issues) > 100
            or any(not isinstance(issue, str) or not issue or len(issue) > 200
                   for issue in self.snapshot.issues)
        ):
            raise ValueError("Invalid clan-health report issues")
        object.__setattr__(self, "retained_bytes", len(json.dumps(
            self.storage_payload(), ensure_ascii=False,
        ).encode("utf-8")))

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "guild_id": self.guild_id,
            "snapshot": asdict(self.snapshot),
        }

    def manifest(self) -> dict[str, Any]:
        status_counts = Counter(row.status for row in self.snapshot.rows)
        totals = {
            name: sum(getattr(row, name) for row in self.snapshot.rows)
            for name in _TOTAL_FIELDS
        }
        return {
            "report_id": self.report_id, "kind": "clan_health",
            "clan_code": self.snapshot.clan_code,
            "run": asdict(self.snapshot.run),
            "total_players": len(self.snapshot.rows),
            "status_counts": dict(sorted(status_counts.items())),
            "totals": totals, "issues": list(self.snapshot.issues),
            "complete_report": True,
        }

    def page(self, *, offset: int = 0, limit: int = 25) -> dict[str, Any]:
        rows = self.snapshot.rows[offset:offset + limit]
        return {
            **self.manifest(), "players": [asdict(row) for row in rows],
            "next_offset": offset + limit if offset + limit < len(self.snapshot.rows) else None,
        }


def compare_clan_health_reports(
    before: ClanHealthReport, after: ClanHealthReport, *, offset: int = 0,
    limit: int = 25,
) -> dict[str, Any]:
    if before.guild_id != after.guild_id:
        raise ValueError("Clan-health reports belong to different servers")
    if before.snapshot.clan_code != after.snapshot.clan_code:
        raise ValueError("Clan-health reports belong to different clans")
    before_rows = {row.player_tag: row for row in before.snapshot.rows}
    after_rows = {row.player_tag: row for row in after.snapshot.rows}
    changes = []
    counts = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
    transitions: Counter[str] = Counter()
    for player_tag in sorted(set(before_rows) | set(after_rows)):
        old = before_rows.get(player_tag)
        new = after_rows.get(player_tag)
        if old is None:
            counts["added"] += 1
            changes.append({"change": "added", "player_tag": player_tag,
                            "before": None, "after": asdict(new)})
            continue
        if new is None:
            counts["removed"] += 1
            changes.append({"change": "removed", "player_tag": player_tag,
                            "before": asdict(old), "after": None})
            continue
        changed_fields = [
            name for name in _COMPARISON_FIELDS
            if getattr(old, name) != getattr(new, name)
        ]
        if not changed_fields:
            counts["unchanged"] += 1
            continue
        counts["changed"] += 1
        if old.status != new.status:
            transitions[f"{old.status} -> {new.status}"] += 1
        deltas = {}
        for name in _DELTA_FIELDS:
            old_value, new_value = getattr(old, name), getattr(new, name)
            if name in changed_fields and old_value is not None and new_value is not None:
                deltas[name] = new_value - old_value
        changes.append({
            "change": "changed", "player_tag": player_tag,
            "changed_fields": changed_fields, "deltas": deltas,
            "before": asdict(old), "after": asdict(new),
        })
    order = {"changed": 0, "added": 1, "removed": 2}
    changes.sort(key=lambda item: (
        order[item["change"]],
        str((item.get("after") or item.get("before") or {}).get("player_name", "")).casefold(),
        item["player_tag"],
    ))
    before_manifest = before.manifest()
    after_manifest = after.manifest()
    return {
        "before": before_manifest, "after": after_manifest,
        "identity_scope": "stored_account_tag_within_selected_clan_report",
        "counts": counts, "status_transitions": dict(sorted(transitions.items())),
        "aggregate_deltas": {
            name: after_manifest["totals"][name] - before_manifest["totals"][name]
            for name in _TOTAL_FIELDS
        },
        "total_changes": len(changes),
        "changes": changes[offset:offset + limit],
        "next_offset": offset + limit if offset + limit < len(changes) else None,
        "complete_comparison": True,
        "interpretation": (
            "Added and removed mean presence in these stored clan reports; they do not by "
            "themselves prove a clan join, departure, ownership change, or inactivity."
        ),
    }


def _validate_row(row: ClanHealthPlayerRow, clan_code: str) -> None:
    if (
        not all(isinstance(value, str) for value in (
            row.player_tag, row.player_name, row.clan_code, row.status, row.note,
        ))
        or normalize_player_tag(row.player_tag) != row.player_tag
        or row.clan_code != clan_code or not row.player_name or not row.status
        or any(not isinstance(flag, str) or not flag for flag in row.flags)
        or type(row.raid_expected_estimated) is not bool
    ):
        raise ValueError("Invalid clan-health player row")
    for name in _TOTAL_FIELDS:
        value = getattr(row, name)
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("Invalid clan-health numeric evidence")
        elif type(value) is not int:
            raise ValueError("Invalid clan-health numeric evidence")
        if value < 0:
            raise ValueError("Invalid clan-health numeric evidence")
    for name in ("townhall", "hero_sum", "games_total", "hero_delta", "capital_delta", "th_delta", "games_delta"):
        value = getattr(row, name)
        if value is not None and type(value) is not int:
            raise ValueError("Invalid optional clan-health metric")
    if any(value is not None and value < 0 for value in (
        row.townhall, row.hero_sum, row.games_total,
    )):
        raise ValueError("Invalid optional clan-health metric")


__all__ = ["ClanHealthReport", "compare_clan_health_reports"]

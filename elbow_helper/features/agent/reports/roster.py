"""Complete stored roster snapshots with bounded model-facing pages."""

from dataclasses import asdict, dataclass, field
import json
from typing import Any

from elbow_helper.features.rosters.models import Roster, RosterSnapshot


def roster_summary(roster: Roster) -> dict[str, Any]:
    return {
        "roster_id": roster.id, "name": roster.name, "clan_code": roster.clan_code,
        "active_cycle_id": roster.active_cycle_id, "status": roster.status,
        "max_accounts": roster.max_members, "min_townhall": roster.min_townhall,
    }


@dataclass(frozen=True, slots=True)
class RosterReport:
    report_id: str
    snapshot: RosterSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        payload = {"report_id": self.report_id, "snapshot": asdict(self.snapshot)}
        object.__setattr__(self, "retained_bytes", len(json.dumps(payload, ensure_ascii=False).encode("utf-8")))

    def manifest(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "kind": "roster_signups",
            "roster_id": self.snapshot.roster.id, "name": self.snapshot.roster.name,
            "cycle_id": self.snapshot.cycle.id if self.snapshot.cycle else None,
            "observed_ts": self.snapshot.observed_ts,
            "total_accounts": len(self.snapshot.members),
            "total_members": len({
                member.discord_user_id for member in self.snapshot.members
            }),
        }

    def page(self, *, offset: int = 0, limit: int = 25) -> dict[str, Any]:
        snapshot = self.snapshot
        return {
            **self.manifest(), "roster": roster_summary(snapshot.roster),
            "cycle": asdict(snapshot.cycle) if snapshot.cycle else None,
            "settings_scope": "current_at_snapshot",
            "membership_scope": "selected_cycle_stored_signups",
            "total_accounts": len(snapshot.members),
            "total_members": len({member.discord_user_id for member in snapshot.members}),
            "accounts": [asdict(member) for member in snapshot.members[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(snapshot.members) else None,
            "complete_snapshot": True,
        }


def compare_roster_reports(
    before: RosterReport, after: RosterReport, *, offset: int = 0, limit: int = 25,
) -> dict[str, Any]:
    """Compare complete retained signups, not current clan membership.

    Signup timestamps are deliberately excluded: signing up again in a new
    cycle is not an account/profile change. No historical settings are inferred.
    """
    if before.snapshot.roster.guild_id != after.snapshot.roster.guild_id:
        raise ValueError("Roster snapshots must belong to the same server")
    if offset < 0 or not 1 <= limit <= 25:
        raise ValueError("Invalid comparison page")
    fields = ("discord_user_id", "player_name", "clan_code", "townhall", "hero_sum")
    previous = {member.player_tag: member for member in before.snapshot.members}
    following = {member.player_tag: member for member in after.snapshot.members}
    if len(previous) != len(before.snapshot.members) or len(following) != len(after.snapshot.members):
        raise ValueError("Roster snapshots contain duplicate account tags")
    counts = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
    rows = []
    for tag in sorted(previous.keys() | following.keys()):
        old, new = previous.get(tag), following.get(tag)
        changed_fields = [name for name in fields if old and new and getattr(old, name) != getattr(new, name)]
        kind = "added" if old is None else "removed" if new is None else "changed" if changed_fields else "unchanged"
        counts[kind] += 1
        if kind != "unchanged":
            rows.append({
                "player_tag": tag, "change": kind, "changed_fields": changed_fields,
                "before": asdict(old) if old else None,
                "after": asdict(new) if new else None,
            })
    def identity(report: RosterReport) -> dict[str, Any]:
        return {
            **report.manifest(),
            "cycle": asdict(report.snapshot.cycle) if report.snapshot.cycle else None,
            "total_accounts": len(report.snapshot.members),
            "total_members": len({member.discord_user_id for member in report.snapshot.members}),
        }
    return {
        "before": identity(before), "after": identity(after),
        "membership_scope": "selected_cycle_stored_signups",
        "compared_fields": list(fields), "ignored_fields": ["signed_up_ts"],
        "counts": counts, "total_changes": len(rows),
        "changes": rows[offset:offset + limit],
        "next_offset": offset + limit if offset + limit < len(rows) else None,
        "complete_comparison": True,
    }

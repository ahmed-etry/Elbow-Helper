"""Observed clan stays, bounded transitions and generation-aware locations."""

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag

from .core import RecordReads, timestamp
from .coverage import signal, envelope
from .profiles import leaf_state, roster_fields


def stays(connection, tag, at):
    """Combine adjacent role intervals only when they describe the same stay."""
    result = []
    for row in connection.execute(
            "SELECT m.*,c.tag AS clan_tag,c.family_code,a.player_tag FROM clan_membership m "
            "JOIN accounts a ON a.id=m.account_id JOIN clans c ON c.id=m.clan_id "
            "WHERE a.player_tag=? AND m.from_at<=? ORDER BY m.from_at,m.id", (tag, at)):
        end = row["to_at"] if row["to_at"] is not None and row["to_at"] <= at else None
        good = connection.execute("SELECT MAX(fetched_at) FROM collection_items WHERE source_kind='clan_roster' "
                                  "AND scope_id=? AND status IN ('ok','empty') AND fetched_at<=?", (row["clan_tag"], at)).fetchone()[0]
        role = {"role": row["clan_role"], "from_at": row["from_at"], "to_at": end}
        if (result and result[-1]["clan_tag"] == row["clan_tag"]
                and result[-1]["to_at"] == row["from_at"] and result[-1]["end_basis"] == "role_change"):
            stay = result[-1]
            stay["roles"].append(role)
        else:
            stay = {"player_tag": tag, "clan_tag": row["clan_tag"], "clan_code": row["family_code"],
                    "from_at": row["from_at"], "previous_good_at": row["previous_good_at"],
                    "first_observed_is_first_ever": False, "roles": [role]}
            result.append(stay)
        stay.update(to_at=end, end_previous_good_at=row["end_previous_good_at"] if end is not None else None,
                    last_confirmed_at=min(row["last_observed_at"], good or row["from_at"]),
                    end_basis=row["end_basis"] if end is not None else None)
    return result


class MembershipReads(RecordReads):
    def __init__(self, store, *, clans=None):
        super().__init__(store)
        self.clans = tuple(CLANS.values() if clans is None else clans)
        self.expected_tags = tuple(clan.tag for clan in self.clans)

    def _clan_tags(self, selected):
        if selected is None:
            return self.expected_tags
        selected = tuple(selected)
        if len(selected) > 200:
            raise ValueError("Expected at most 200 clans")
        by_code = {clan.code: clan.tag for clan in self.clans}
        values = [by_code.get(value) or normalize_player_tag(value) for value in selected]
        if any(value is None for value in values):
            raise ValueError("Unknown clan")
        return tuple(sorted(set(values)))

    async def rosters(self, *, clans=None, as_of=None):
        """One record snapshot for the account-link boards and suggestions.

        Failed, partial and stale sources remain unavailable. Callers may keep
        their last snapshot but cannot use it to prove family-wide absence.
        """
        return await self._read(self._rosters, self._clan_tags(clans), timestamp(as_of))

    @staticmethod
    def _rosters(connection, selected, at):
        rows, runs = [], set()
        for tag in selected:
            cov = signal(connection, "clan_roster", tag, at)
            item = connection.execute("SELECT run_id,fetched_at FROM collection_items WHERE source_kind='clan_roster' "
                                      "AND scope_id=? AND COALESCE(fetched_at,started_at)<=? "
                                      "ORDER BY COALESCE(fetched_at,started_at) DESC,id DESC LIMIT 1", (tag, at)).fetchone()
            if item:
                runs.add(item["run_id"])
            fields = leaf_state(connection, "clan_profile", tag, at)
            badge = next((fields.get('/badgeUrls/' + size) for size in ('small', 'medium', 'large')
                          if fields.get('/badgeUrls/' + size)), None)
            members = None
            if cov["source_complete"]:
                roster = roster_fields(connection, tag, cov["last_good_at"])
                members = [{"player_tag": member_tag,
                            "player_name": str(value.get("name") or member_tag),
                            "townhall": int(value.get("townHallLevel") or 0),
                            "role": str(value.get("role") or "")}
                           for member_tag, value in roster.items()]
            rows.append({"clan_tag": tag, "members": members, "badge_url": badge, "coverage": cov,
                         "observed_at": cov["last_good_at"]})
        complete = bool(rows) and len(runs) == 1 and all(row["members"] is not None for row in rows)
        coverage = envelope([row["coverage"] for row in rows], at=at)
        coverage["complete"] &= complete
        return {"rows": rows, "complete": complete, "coverage": coverage}

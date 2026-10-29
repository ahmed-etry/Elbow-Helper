"""Observed clan stays, bounded transitions and generation-aware locations."""

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag

from .core import RecordReads, tags as checked_tags, timestamp, pagination, page, placeholders
from .coverage import signal, envelope
from .people import ownership
from .profiles import summary, leaf_state


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

    async def current(self, *, clans=None, tags=None, as_of=None, limit=200, offset=0):
        return await self.at(timestamp(as_of), clans=clans, tags=tags, limit=limit, offset=offset)

    async def at(self, time, *, clans=None, tags=None, limit=200, offset=0):
        pagination(limit, offset)
        return await self._read(self._at, timestamp(time), self._clan_tags(clans),
                                checked_tags(tags) if tags is not None else None, limit, offset)

    def _at(self, connection, at, selected_clans, selected_tags, limit, offset):
        coverages = {tag: signal(connection, "clan_roster", tag, at) for tag in
                     dict.fromkeys((*self.expected_tags, *selected_clans))}
        candidates = []
        if selected_tags is not None:
            candidates = list(selected_tags)
        elif selected_clans:
            candidates = [row[0] for row in connection.execute(
                "SELECT a.player_tag FROM clan_membership m JOIN accounts a ON a.id=m.account_id "
                "JOIN clans c ON c.id=m.clan_id "
                f"WHERE c.tag IN ({placeholders(selected_clans)}) AND m.from_at<=? AND (m.to_at IS NULL OR m.to_at>?) "
                "ORDER BY a.player_tag LIMIT ? OFFSET ?", (*selected_clans, at, at, limit + 1, offset))]
        result = page(candidates[offset:offset + limit + 1] if selected_tags is not None else candidates, limit, offset)
        cache, rows = {}, []
        for tag in result["rows"]:
            account = summary(connection, tag, at, cache)
            history = stays(connection, tag, at)
            active = next((s for s in reversed(history) if s["to_at"] is None or s["to_at"] > at), None)
            row = {"player_tag": tag, "player_name": account["player_name"],
                   "townHallLevel": account["townHallLevel"], "clan_tag": None, "clan_code": None,
                   "role": None, "status": "unknown_location", "stay": active,
                   "ownership": ownership(connection, tag, at), "profile": account,
                   "coverage": None}
            if active:
                cov = coverages.setdefault(active["clan_tag"], signal(connection, "clan_roster", active["clan_tag"], at))
                role = next(r for r in reversed(active["roles"]) if r["from_at"] <= at and
                            (r["to_at"] is None or r["to_at"] > at))
                row.update(clan_tag=active["clan_tag"], clan_code=active["clan_code"], role=role["role"],
                           status="observed_clan" if cov["source_complete"] else "last_known_clan")
                row["stay"] = {**active, "roles": [role]}
                # Profiles can still observe a linked account outside the
                # family while its old clan is unavailable. Surface both
                # sources rather than claiming that carried stay is current.
                location = leaf_state(connection, "player_profile", tag, at)
                profile_at = connection.execute("SELECT slow_sampled_at FROM player_collection_state WHERE account_id=("
                                                "SELECT id FROM accounts WHERE player_tag=?)", (tag,)).fetchone()
                sampled_at = profile_at[0] if profile_at and profile_at[0] is not None and profile_at[0] <= at else None
                if location and sampled_at is not None:
                    clan = account["fields"].get("clan")
                    row["profile_location"] = {"clan": clan, "sampled_at": sampled_at}
                    if sampled_at > (cov["last_good_at"] or 0) and (clan or {}).get("tag") != active["clan_tag"]:
                        row["status"] = "conflicting_observations"
                row["coverage"] = envelope([cov, *account["signals"]], at=at)
            else:
                profile_cov = account["coverage"]
                # Clanless and outside-family profiles are positive evidence
                # only at their slow sample. They never manufacture a stay.
                fields = leaf_state(connection, "player_profile", tag, at)
                if fields and profile_cov["last_good_at"] is not None:
                    clan = account["fields"].get("clan")
                    row.update(profile_clan=clan, status="profile_clan_observed" if clan else "no_clan_observed")
                elif history:
                    row["status"] = "unknown_destination"
                row["coverage"] = envelope([*coverages.values(), *account["signals"]], at=at)
            rows.append(row)
        result["rows"] = rows
        result["clans"] = [{"clan_tag": tag, "coverage": coverages[tag]} for tag in selected_clans]
        result["coverage"] = envelope([coverages[tag] for tag in selected_clans], at=at, truncated=result["truncated"])
        # Whole-family absence requires one complete generation of every
        # configured scope. Per-clan success times are never refreshed here.
        latest = [connection.execute("SELECT run_id,status FROM collection_items WHERE source_kind='clan_roster' "
                                     "AND scope_id=? AND COALESCE(fetched_at,started_at)<=? "
                                     "ORDER BY COALESCE(fetched_at,started_at) DESC,id DESC LIMIT 1",
                                     (tag, at)).fetchone() for tag in self.expected_tags]
        result["family_generation_complete"] = bool(latest) and all(
            row and row["status"] in {"ok", "empty"} and coverages[tag]["source_complete"]
            for tag, row in zip(self.expected_tags, latest)) and len({row["run_id"] for row in latest if row}) == 1
        return result

    async def history(self, after, before, *, clans=None, tags=None, limit=200, offset=0):
        after, before = timestamp(after), timestamp(before)
        pagination(limit, offset)
        if after >= before:
            raise ValueError("The history window must end after it starts")
        return await self._read(self._history, after, before, self._clan_tags(clans),
                                checked_tags(tags) if tags is not None else None, limit, offset)

    async def generations(self, after, before):
        """The latest 128 complete family generations inside a history window."""
        return await self._read(self._generations, timestamp(after), timestamp(before))

    def _generations(self, connection, after, before):
        scopes = self.expected_tags
        if not scopes:
            return {'rows': [], 'truncated': False}
        rows = connection.execute(
            'SELECT run_id,MIN(fetched_at) AS at FROM collection_items '
            f"WHERE source_kind='clan_roster' AND scope_id IN ({placeholders(scopes)}) "
            'AND fetched_at>=? AND fetched_at<? GROUP BY run_id '
            "HAVING COUNT(*)=? AND COUNT(DISTINCT scope_id)=? AND SUM(status IN ('ok','empty') "
            "AND json_extract(completeness_json,'$.complete')=1)=? ORDER BY at DESC,run_id DESC LIMIT 129",
            (*scopes, after, before, len(scopes), len(scopes), len(scopes))).fetchall()
        return {'rows': [dict(row) for row in reversed(rows[:128])], 'truncated': len(rows) > 128}

    @staticmethod
    def _history(connection, after, before, selected_clans, selected_tags, limit, offset):
        # Filtering and coalescing happen before paging, so role splits never
        # make a stay disappear or reappear at a page boundary.
        values = []
        params = [before]
        where = "m.from_at<?"
        if selected_tags is not None:
            where += f" AND a.player_tag IN ({placeholders(selected_tags)})" if selected_tags else " AND 0"
            params.extend(selected_tags)
        cursor = connection.execute("SELECT DISTINCT a.player_tag FROM clan_membership m "
                                    "JOIN accounts a ON a.id=m.account_id WHERE " + where + " ORDER BY a.player_tag", params)
        for row in cursor:
            prior = None
            seen_clans = set()
            for stay in stays(connection, row[0], before - 1):
                kind = ("transfer" if prior and prior["to_at"] == stay["from_at"] and
                        prior["end_basis"] == "observed_destination" else
                        "return" if prior and prior["clan_tag"] == stay["clan_tag"] else
                        "observed_join" if prior or stay["previous_good_at"] is not None else "first_observed")
                stay["start_kind"] = kind
                stay["is_return"] = stay["clan_tag"] in seen_clans
                stay["from_clan_tag"] = prior["clan_tag"] if kind == "transfer" else None
                stay["events"] = []
                if after <= stay["from_at"] < before:
                    stay["events"].append({"kind": kind, "last_confirmed_at": stay["previous_good_at"],
                                           "first_observed_at": stay["from_at"], "exact": False})
                if stay["to_at"] is not None and after <= stay["to_at"] < before:
                    stay["events"].append({"kind": "transfer_out" if stay["end_basis"] == "observed_destination" else "observed_absence",
                                           "last_confirmed_at": stay["end_previous_good_at"],
                                           "first_observed_at": stay["to_at"], "exact": False})
                if (stay["clan_tag"] in selected_clans and stay["from_at"] < before
                        and (stay["to_at"] is None or stay["to_at"] > after or stay["events"])):
                    stay["coverage"] = signal(connection, "clan_roster", stay["clan_tag"], before - 1, after=after, before=before)
                    stay['player_name'] = summary(connection, stay['player_tag'], max(after, stay['from_at']))['player_name']
                    relevant_roles = [r for r in stay["roles"] if r["from_at"] < before and
                                      (r["to_at"] is None or r["to_at"] > after)]
                    stay["roles_truncated"] = len(relevant_roles) > 200
                    stay["roles"] = relevant_roles[:200]
                    values.append(stay)
                seen_clans.add(stay["clan_tag"])
                prior = stay
        values.sort(key=lambda row: (row["from_at"], row["player_tag"]))
        result = page(values[offset:offset + limit + 1], limit, offset)
        coverages = [signal(connection, "clan_roster", tag, before - 1, after=after, before=before) for tag in selected_clans]
        result["coverage"] = envelope(coverages, after=after, before=before,
                                      truncated=result["truncated"] or any(s["roles_truncated"] for s in result["rows"]))
        return result

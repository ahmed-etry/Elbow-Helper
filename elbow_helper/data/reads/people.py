"""Dated ownership, Discord role cohorts and two-sided identity resolution."""

import sqlite3
from types import SimpleNamespace

# Reuse the existing Unicode comparisons verbatim during the staged move.
from elbow_helper.agent.families.account_names import matching_accounts
from elbow_helper.agent.research.member_identity import identity_query, matches_discord_identity
from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag

from .core import RecordReads, identifiers, tags, timestamp, pagination, page, placeholders
from .coverage import signal, envelope
from .profiles import summary, account_history


def ownership(connection, tag, at):
    row = connection.execute(
        "SELECT l.*,m.discord_id FROM account_links l JOIN accounts a ON a.id=l.account_id "
        "JOIN members m ON m.id=l.member_id WHERE a.player_tag=? AND l.from_at<=? "
        "AND (l.to_at IS NULL OR l.to_at>?)", (tag, at, at)).fetchone()
    coverage = signal(connection, "account_links", "current", at)
    result = {"player_tag": tag, "member_id": None, "status": "unknown", "interval": None,
              "coverage": coverage}
    if row:
        confirmed = min(row["last_observed_at"], coverage["last_good_at"] or row["from_at"])
        result.update(member_id=row["discord_id"], status="last_observed_owner", interval={
            "from_at": row["from_at"], "to_at": None, "basis": row["basis"],
            "previous_good_at": row["previous_good_at"], "end_previous_good_at": None,
            "last_confirmed_at": confirmed,
            "exact_start": row["basis"] == "local_operation",
            "confidence": "observed_at" if confirmed == at else "last_known",
            "continuous_ownership_known": False})
    elif coverage["last_good_at"] is not None:
        result["status"] = "unlinked_at_last_observation"
    return result


def member_row(connection, identifier, at, clans=None):
    member = connection.execute("SELECT * FROM members WHERE discord_id=? AND first_observed_at<=?",
                                (identifier, at)).fetchone()
    result = {"member_id": identifier, "username": None, "member_name": None, "nickname": None,
              "roles": [], "role_intervals": [], "home_clans": [], "participation_clans": [],
              "presence": "unknown", "first_observed_at": member["first_observed_at"] if member else None}
    if not member:
        return result
    for row in connection.execute("SELECT * FROM member_aliases WHERE member_id=? AND from_at<=? "
                                  "AND (to_at IS NULL OR to_at>?)", (member["id"], at, at)):
        field = {"username": "username", "display_name": "member_name", "nick": "nickname"}.get(row["kind"])
        if field:
            result[field] = row["name"]
    role_rows = connection.execute(
        "SELECT role_id,from_at,previous_good_at,observation_basis FROM member_roles "
        "WHERE member_id=? AND from_at<=? AND (to_at IS NULL OR to_at>?) ORDER BY role_id", (member["id"], at, at)).fetchall()
    result["roles"] = [row["role_id"] for row in role_rows]
    result["role_intervals"] = [dict(row) for row in role_rows]
    for clan in CLANS.values() if clans is None else clans:
        if getattr(clan, "member_role_id", None) in result["roles"]:
            field = "participation_clans" if getattr(clan, "is_utility", False) else "home_clans"
            result[field].append({"clan_code": clan.code, "clan_tag": clan.tag, "role_id": clan.member_role_id})
    stay = connection.execute("SELECT * FROM guild_membership WHERE member_id=? AND from_at<=? "
                              "ORDER BY from_at DESC LIMIT 1", (member["id"], at)).fetchone()
    if stay:
        result["presence"] = "observed_present" if stay["to_at"] is None or stay["to_at"] > at else "observed_departed"
    return result


class PeopleReads(RecordReads):
    """All selectors are bounded to 200; all disk/alias-adapter work is threaded.

    ``alias_reader`` is the read-only ``cwl_queries.account_names`` callable.
    Its undated names never establish ownership or historical membership.
    """

    def __init__(self, store, *, alias_reader=None, clans=None, link_service=None):
        super().__init__(store)
        self.alias_reader = alias_reader
        self.clans = tuple(CLANS.values() if clans is None else clans)
        self.clan_tags = tuple(clan.tag for clan in self.clans)
        self.link_service = link_service

    async def account_history(self, player_tags, as_of=None):
        selected, at = tags(player_tags), timestamp(as_of)
        if self.link_service is not None:
            # Ownership history must be read between completed link mutations.
            return await self.link_service.read_people(self._snapshot, self._account_history, (selected, at))
        return await self._read(self._account_history, selected, at)

    @staticmethod
    def _account_history(connection, selected, at):
        return {'rows': [{'player_tag': tag, 'record_history': account_history(connection, tag, at)}
                         for tag in selected]}

    async def owners(self, player_tags, as_of=None):
        return await self._read(self._owners, tags(player_tags), timestamp(as_of))

    @staticmethod
    def _owners(connection, selected, at):
        rows = [ownership(connection, tag, at) for tag in selected]
        return {"rows": rows, "coverage": envelope([signal(connection, "account_links", "current", at)], at=at)}

    async def accounts(self, member_ids, as_of=None, *, limit=200, offset=0):
        """Page ownership rows; every requested member appears on every page.

        An empty page list is conclusive only when coverage is complete and
        offset is zero with no truncation. Continue with next_offset for alts.
        """
        pagination(limit, offset)
        return await self._read(self._accounts, identifiers(member_ids), timestamp(as_of), limit, offset)

    def _accounts(self, connection, selected, at, limit, offset):
        links = []
        if selected:
            links = connection.execute(
                "SELECT m.discord_id,a.player_tag FROM account_links l JOIN accounts a ON a.id=l.account_id "
                "JOIN members m ON m.id=l.member_id "
                f"WHERE m.discord_id IN ({placeholders(selected)}) AND l.from_at<=? AND (l.to_at IS NULL OR l.to_at>?) "
                "ORDER BY m.discord_id,a.player_tag LIMIT ? OFFSET ?", (*selected, at, at, limit + 1, offset)).fetchall()
        pagination_result = page(links, limit, offset)
        rows = {identifier: {**member_row(connection, identifier, at, self.clans), "accounts": []} for identifier in selected}
        cache = {}
        for link in pagination_result.pop("rows"):
            rows[link["discord_id"]]["accounts"].append({**summary(connection, link["player_tag"], at, cache),
                                                        "ownership": ownership(connection, link["player_tag"], at)})
        cov = signal(connection, "account_links", "current", at)
        discord_cov = signal(connection, "discord_scan", None, at)
        for row in rows.values():
            row["coverage"] = cov
            row["discord_coverage"] = discord_cov
            row["accounts_complete"] = cov["source_complete"] and not pagination_result["truncated"] and offset == 0
        return {"rows": list(rows.values()), **pagination_result,
                "coverage": envelope([cov, discord_cov, *(s for row in rows.values() for a in row["accounts"]
                                                           for s in a["signals"])],
                                     at=at, truncated=pagination_result["truncated"])}

    async def account_summary(self, player_tags, as_of=None):
        return await self._read(self._summaries, tags(player_tags), timestamp(as_of))

    @staticmethod
    def _summaries(connection, selected, at):
        cache = {}
        rows = [summary(connection, tag, at, cache) for tag in selected]
        return {"rows": rows, "coverage": envelope([s for row in rows for s in row["signals"]], at=at)}

    async def cohort(self, role_ids, as_of=None, *, match="any", limit=200, offset=0):
        pagination(limit, offset)
        if match not in {"any", "all"}:
            raise ValueError("Expected any or all roles")
        return await self._read(self._cohort, identifiers(role_ids), timestamp(as_of), match, limit, offset)

    def _cohort(self, connection, selected, at, match, limit, offset):
        rows = []
        if selected:
            rows = connection.execute(
                "SELECT m.discord_id FROM member_roles r JOIN members m ON m.id=r.member_id "
                f"WHERE r.role_id IN ({placeholders(selected)}) AND r.from_at<=? AND (r.to_at IS NULL OR r.to_at>?) "
                "GROUP BY m.discord_id HAVING COUNT(DISTINCT r.role_id)>=? ORDER BY m.discord_id LIMIT ? OFFSET ?",
                (*selected, at, at, len(selected) if match == "all" else 1, limit + 1, offset)).fetchall()
        result = page(rows, limit, offset)
        result["rows"] = [member_row(connection, row[0], at, self.clans) for row in result["rows"]]
        cov = signal(connection, "discord_scan", None, at)
        for row in result["rows"]:
            row["coverage"] = cov
        result["coverage"] = envelope([cov], at=at, truncated=result["truncated"])
        result["role_ids"], result["role_match"] = list(selected), match
        return result

    async def resolve(self, query, as_of=None, *, limit=200, offset=0):
        pagination(limit, offset)
        if not isinstance(query, str) or not query.strip() or len(query) > 100:
            raise ValueError("Expected a name, tag, ID or mention of 1..100 characters")
        return await self._read(self._resolve, query.strip(), timestamp(as_of), limit, offset)

    def _resolve(self, connection, query, at, limit, offset):
        accounts, members, missing = {}, {}, []

        def add(tag, name, source, since=None):
            tag = normalize_player_tag(tag)
            if tag is None or not isinstance(name, str) or not name.strip():
                return
            row = accounts.setdefault(tag, {"player_tag": tag, "names": [], "aliases": []})
            if name not in row["names"]:
                row["names"].append(name)
            row["aliases"].append({"name": name, "source": source, "observed_from": since})

        for row in connection.execute("SELECT a.player_tag,n.name,n.from_at FROM account_aliases n "
                                      "JOIN accounts a ON a.id=n.account_id WHERE n.from_at<=?", (at,)):
            add(row["player_tag"], row["name"], "family_record", row["from_at"])
        # Metadata has unknown age. It is searchable evidence, never a dated
        # ownership fact, and is included only once its receipt was observed.
        for row in connection.execute("SELECT a.player_tag,m.player_name_last_seen FROM account_link_metadata m "
                                      "JOIN accounts a ON a.id=m.account_id JOIN collection_items i ON i.id=m.source_item_id "
                                      "WHERE i.fetched_at<=?", (at,)):
            add(row["player_tag"], row["player_name_last_seen"], "legacy_link_metadata")
        if self.alias_reader is None:
            missing.append({"source": "stored_account_directory", "cause": "missing_adapter"})
        else:
            try:
                aliases = self.alias_reader()
                if isinstance(aliases, dict):
                    aliases = aliases.values()
                for row in aliases:
                    add(row.get("player_tag"), row.get("player_name"), "stored_account_directory")
            except (OSError, sqlite3.Error, TimeoutError):
                missing.append({"source": "stored_account_directory", "cause": "unavailable"})
        explicit, _ = identity_query(query)
        for row in connection.execute("SELECT discord_id,id FROM members WHERE first_observed_at<=?", (at,)):
            aliases = [r[0] for r in connection.execute("SELECT name FROM member_aliases WHERE member_id=? AND from_at<=?",
                                                       (row["id"], at))]
            candidate = SimpleNamespace(id=row["discord_id"], aliases=aliases)
            if matches_discord_identity(candidate, query):
                members[row["discord_id"]] = member_row(connection, row["discord_id"], at, self.clans)
        tag = normalize_player_tag(query)
        if tag and (query.startswith("#") or tag in accounts):
            exists = connection.execute("SELECT 1 FROM accounts WHERE player_tag=? AND first_observed_at<=?", (tag, at)).fetchone()
            found = [accounts.get(tag, {"player_tag": tag, "names": [], "aliases": []})] if exists or tag in accounts else []
        else:
            found = [] if explicit else matching_accounts(query, accounts)
        rows = [{"kind": "member", **row} for _, row in sorted(members.items())]
        for candidate in found:
            rows.append({"kind": "account", **candidate, "ownership": ownership(connection, candidate["player_tag"], at)})
        result = page(rows[offset:offset + limit + 1], limit, offset)
        for row in result["rows"]:
            if row["kind"] == "account":
                row["aliases_truncated"] = len(row["aliases"]) > 200 or len(row["names"]) > 200
                row["aliases"], row["names"] = row["aliases"][:200], row["names"][:200]
        owners = {row["ownership"]["member_id"] for row in rows if row["kind"] == "account"}
        identities = set(members) | (owners - {None})
        ambiguous = len(found) > 1 or len(identities) > 1 or bool(members and None in owners)
        cov = [signal(connection, kind, scope, at) for kind, scope in
               (("account_links", "current"), ("discord_scan", None))]
        cov.extend(signal(connection, "clan_roster", tag, at) for tag in self.clan_tags)
        result.update(status="ambiguous" if ambiguous else "resolved" if rows else
                      "unknown" if missing or any(not c["source_complete"] for c in cov) else "not_found",
                      missing_sources=missing, candidate_count=len(rows),
                      coverage=envelope(cov, at=at, truncated=result["truncated"]))
        result["coverage"]["missing_sources"] = missing
        result["coverage"]["truncated"] |= any(r.get("aliases_truncated", False) for r in result["rows"])
        if missing:
            result["coverage"]["complete"] = False
        return result

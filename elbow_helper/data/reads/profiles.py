"""Identity projection shared by people and membership reads; no HTTP fetches."""

import json

from ..store.api_fields import unpack
from ..store.player_projection import COUNTERS
from .coverage import signal
from .core import MAX_PAGE


def leaf_state(connection, kind, scope, at):
    cache = getattr(connection, "leaf_cache", {})
    key = (kind, scope, at)
    if key in cache:
        return cache[key]
    record = connection.execute("SELECT id,state_z FROM api_records WHERE source_kind=? AND scope_id=?",
                                (kind, scope)).fetchone()
    if record is None:
        return {}
    paths, current = unpack(record["state_z"])
    latest = connection.execute("SELECT MAX(observed_at) FROM api_field_changes WHERE record_id=?", (record["id"],)).fetchone()[0]
    if latest is not None and latest <= at:
        result = {paths[int(slot)]: value[0] for slot, value in current.items()}
        cache[key] = result
        return result
    state = {}
    for row in connection.execute(
            "SELECT patch_z FROM api_field_changes WHERE record_id=? AND observed_at<=? ORDER BY observed_at,source_run_id",
            (record["id"], at)):
        changed, removed = unpack(row[0])
        state.update(changed)
        for slot in removed:
            state.pop(slot, None)
    result = {paths[int(slot)]: value for slot, value in state.items()}
    cache[key] = result
    return result


def roster_fields(connection, clan_tag, at):
    values = {}
    for path, value in leaf_state(connection, "clan_profile", clan_tag, at).items():
        parts = [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]]
        if len(parts) != 3 or parts[0] != "memberList" or not parts[1].startswith("@"):
            continue
        identity = json.loads(parts[1][1:])
        tag = identity.get("tag")
        if tag:
            values.setdefault(tag, {})[parts[2]] = value
    return values


def summary(connection, tag, at, cache=None):
    account = connection.execute("SELECT * FROM accounts WHERE player_tag=? AND first_observed_at<=?", (tag, at)).fetchone()
    coverage = signal(connection, "player_profile", tag, at, account_id=account["id"] if account else None)
    fields, observed, sources, metadata = {}, {}, {}, None
    signals = [coverage]
    if account:
        for row in connection.execute(
                "SELECT p.* FROM player_observations p WHERE account_id=? AND observed_at<=? AND observed_at=("
                "SELECT MAX(observed_at) FROM player_observations WHERE account_id=p.account_id "
                "AND field_key=p.field_key AND observed_at<=?)", (account["id"], at, at)):
            fields[row["field_key"]] = json.loads(row["value_json"])
            observed[row["field_key"]] = row["observed_at"]
            sources[row["field_key"]] = "player_profile"
        counter = connection.execute("SELECT * FROM player_counters WHERE account_id=? AND sampled_at<=? "
                                     "ORDER BY sampled_at DESC LIMIT 1", (account["id"], at)).fetchone()
        if counter:
            presence = json.loads(counter["field_presence_json"]).get("fields", 0)
            for index, (key, column) in enumerate(COUNTERS.items()):
                present = key in presence if isinstance(presence, list) else bool(presence & (1 << index))
                if present:
                    fields[key], observed[key] = counter[column], counter["sampled_at"]
                    sources[key] = "player_profile"
        alias = connection.execute("SELECT n.name,n.from_at,i.source_kind,i.scope_id FROM account_aliases n "
                                   "JOIN collection_items i ON i.id=n.source_item_id WHERE n.account_id=? AND n.from_at<=? "
                                   "ORDER BY n.from_at DESC LIMIT 1", (account["id"], at)).fetchone()
        if alias:
            fields["name"], observed["name"] = alias["name"], alias["from_at"]
            sources["name"] = alias["source_kind"]
            if alias["source_kind"] == "clan_roster":
                signals.append(signal(connection, "clan_roster", alias["scope_id"], at))
        legacy = connection.execute("SELECT m.* FROM account_link_metadata m JOIN collection_items i ON i.id=m.source_item_id "
                                    "WHERE m.account_id=? AND i.fetched_at<=?", (account["id"], at)).fetchone()
        if legacy:
            metadata = {key: legacy[key] for key in ("player_name_last_seen", "last_seen_clan_tag", "last_seen_clan_code",
                                                     "last_seen_role", "observed_at", "basis")}
            if "name" not in fields and legacy["player_name_last_seen"]:
                fields["name"], observed["name"] = legacy["player_name_last_seen"], None
                sources["name"] = "legacy_unknown_age"
        stay = connection.execute(
            "SELECT m.*,c.tag FROM clan_membership m JOIN clans c ON c.id=m.clan_id "
            "WHERE account_id=? AND from_at<=? AND (to_at IS NULL OR to_at>?)",
            (account["id"], at, at)).fetchone()
        if stay:
            roster_coverage = signal(connection, "clan_roster", stay["tag"], at)
            signals.append(roster_coverage)
            good_at = roster_coverage["last_good_at"]
            if good_at is not None:
                cache = {} if cache is None else cache
                key = (stay["tag"], good_at)
                if key not in cache:
                    cache[key] = roster_fields(connection, *key)
                row = cache[key].get(tag, {})
                for field in ("name", "townHallLevel"):
                    if field in row and good_at >= (observed.get(field) or -1):
                        fields[field], observed[field] = row[field], good_at
                        sources[field] = "clan_roster"
            # Membership role/location have a separate signal; do not overwrite
            # profile fields from a different observation date.
    required = ("name", "townHallLevel", "leagueTier", "league", "role", "clan", *COUNTERS)
    return {"player_tag": tag, "player_name": fields.get("name"), "townHallLevel": fields.get("townHallLevel"),
            "ranked_league": fields.get("leagueTier"), "legacy_league": fields.get("league"),
            "role": fields.get("role"), "clan": fields.get("clan"), "fields": fields,
            "field_observed_at": observed, "field_sources": sources,
            "field_presence": {key: key in fields and fields[key] is not None for key in required},
            "first_observed_at": account["first_observed_at"] if account else None,
            "coverage": coverage, "signals": signals, "link_metadata": metadata}


def account_history(connection, tag, at):
    """Bounded dated identity facts, separate from any claim of current state."""
    account = connection.execute('SELECT * FROM accounts WHERE player_tag=? AND first_observed_at<=?',
                                 (tag, at)).fetchone()
    if account is None:
        return None
    identifier = account['id']
    links = [dict(row) for row in connection.execute(
        'SELECT m.discord_id AS member_id,l.from_at,l.to_at,l.previous_good_at,l.end_previous_good_at,'
        'l.last_observed_at,l.basis FROM account_links l JOIN members m ON m.id=l.member_id '
        'WHERE l.account_id=? AND l.from_at<=? ORDER BY l.from_at DESC LIMIT ?', (identifier, at, MAX_PAGE + 1))]
    clans = [dict(row) for row in connection.execute(
        'SELECT c.tag AS clan_tag,c.family_code AS clan_code,m.clan_role AS role,m.from_at,m.to_at,'
        'm.previous_good_at,m.end_previous_good_at,m.last_observed_at,m.end_basis '
        'FROM clan_membership m JOIN clans c ON c.id=m.clan_id WHERE m.account_id=? AND m.from_at<=? '
        'ORDER BY m.from_at DESC LIMIT ?', (identifier, at, MAX_PAGE + 1))]
    aliases = [dict(row) for row in connection.execute(
        'SELECT name,from_at,to_at FROM account_aliases WHERE account_id=? AND from_at<=? '
        'ORDER BY from_at DESC LIMIT ?', (identifier, at, MAX_PAGE + 1))]
    last_seen = [account['first_observed_at']]
    if account['last_profile_at'] is not None and account['last_profile_at'] <= at:
        last_seen.append(account['last_profile_at'])
    for row in [*links, *clans]:
        if row['last_observed_at'] <= at:
            last_seen.append(row['last_observed_at'])
        else:
            row['last_observed_at'] = None
    for row in [*links, *clans, *aliases]:
        if row['to_at'] is not None and row['to_at'] > at:
            row['to_at'] = None
            if 'end_previous_good_at' in row:
                row['end_previous_good_at'] = None
            if 'end_basis' in row:
                row['end_basis'] = None
    legacy = connection.execute(
        'SELECT m.player_name_last_seen FROM account_link_metadata m JOIN collection_items i ON i.id=m.source_item_id '
        'WHERE m.account_id=? AND i.fetched_at<=?', (identifier, at)).fetchone()
    return {'first_observed_at': account['first_observed_at'], 'last_observed_at': max(last_seen),
            'ownership_intervals': links[:MAX_PAGE], 'clan_intervals': clans[:MAX_PAGE],
            'names': aliases[:MAX_PAGE], 'legacy_name': legacy[0] if legacy else None,
            'truncated': any(len(rows) > MAX_PAGE for rows in (links, clans, aliases)),
            'first_observed_is_first_ever': False}

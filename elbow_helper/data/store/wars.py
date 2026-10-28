"""Competition facts, written only on change through the family writer."""

from hashlib import sha256
import json

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction
from . import war_state
from .api_fields import collect_fields
from .projections import canonical_json
from .war_projection import badge, optional_int, project_log_entry, project_war, text


SECOND = 1_000_000


def next_slot(at, seconds):
    """Routine polls share a wall-clock grid, so due clans share one run."""
    step = seconds * SECOND
    return (at // step + 1) * step


def sealed(row):
    return bool(row and row["final_at"] is not None and all(row[key] for key in (
        "roster_complete", "attacks_complete", "result_complete")))


def put(connection, table, keys, values):
    """Preserve provenance and avoid rewriting an unchanged fact."""
    where = " AND ".join(f"{key}=?" for key in keys)
    old = connection.execute(f"SELECT * FROM {table} WHERE {where}", tuple(keys.values())).fetchone() if keys else None
    if old is not None and all(old[key] == value for key, value in values.items() if key != "source_run_id"):
        return old["id"] if "id" in old.keys() else None
    if old is None:
        row = {**keys, **values}
        result = connection.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                                    tuple(row.values()))
        return result.lastrowid
    connection.execute(f"UPDATE {table} SET {','.join(f'{key}=?' for key in values)} WHERE {where}",
                       (*values.values(), *keys.values()))
    return old["id"] if "id" in old.keys() else None


def _account(connection, tag):
    row = connection.execute("SELECT id FROM accounts WHERE player_tag=?", (tag,)).fetchone()
    return row[0] if row else None


def save_war(connection, war, run, at):
    tags = [s["clan_tag"] for s in war["sides"]]
    pair = ":".join(tags)
    keys = [("end", f"{pair}:{war['end_at']}")]
    if war["aggregate"]:
        keys.insert(0, ("war_log", keys[0][1]))
    if war["preparation_at"] is not None:
        keys.insert(0, ("preparation", f"{pair}:{war['preparation_at']}"))
    if war["war_tag"]:
        keys.insert(0, ("cwl", war["war_tag"]))
    war_id = None
    for kind, key in keys:
        if kind in {"end", "war_log"}:
            continue
        row = connection.execute("SELECT war_id FROM war_source_keys WHERE source_kind=? AND source_key=?",
                                 (kind, key)).fetchone()
        if row:
            candidate = connection.execute("SELECT * FROM wars WHERE id=?", (row[0],)).fetchone()
            if war["war_tag"] and candidate["war_tag"] not in (None, war["war_tag"]):
                continue
            war_id = row[0]
            break
    # Match an aggregate backfill only when the clan pair and end are unique.
    matches = {r[0] for r in connection.execute(
            "SELECT w.id FROM wars w JOIN war_sides a ON a.war_id=w.id AND a.side=0 "
            "JOIN war_sides b ON b.war_id=w.id AND b.side=1 WHERE w.end_at=? AND a.clan_tag=? AND b.clan_tag=?",
            (war["end_at"], *tags))}
    if war_id is None and len(matches) == 1:
        war_id = next(iter(matches))
    elif war_id is None and war["aggregate"]:
        unresolved = connection.execute(
            "SELECT w.id FROM war_source_keys k JOIN wars w ON w.id=k.war_id WHERE k.source_kind='war_log' "
            "AND k.source_key=? AND w.preparation_at IS NULL", (keys[-1][1],)).fetchone()
        war_id = unresolved[0] if unresolved else None
    old = connection.execute("SELECT * FROM wars WHERE id=?", (war_id,)).fetchone() if war_id else None
    if old and ((war["preparation_at"] is not None and old["preparation_at"] is not None and war["preparation_at"] != old["preparation_at"])
                or war["war_tag"] and old["war_tag"] not in (None, war["war_tag"])):
        war_id, old = None, None
    for kind, key in keys:
        if war_id and (kind != "end" or len(matches) == 1):
            connection.execute("INSERT OR IGNORE INTO war_source_keys VALUES (?,?,?)", (kind, key, war_id))
    if sealed(old) or old and old["state"] == "warEnded" and war["state"] != "warEnded":
        if war["war_tag"] and old["war_tag"] is None:
            connection.execute("UPDATE wars SET type='CWL',war_tag=?,source_run_id=?,last_seen_at=? WHERE id=?",
                               (war["war_tag"], run, at, war_id))
        elif old["type"] == "UNKNOWN" and war["type"] != "UNKNOWN":
            connection.execute("UPDATE wars SET type=?,source_run_id=?,last_seen_at=? WHERE id=?",
                               (war["type"], run, at, war_id))
        return war_id, sealed(old)
    # An identical poll stops here: no fact, member or attack row is touched.
    digest = sha256(canonical_json(war).encode("utf-8")).hexdigest()
    if old and old["content_hash"] == digest:
        return war_id, bool(old["roster_complete"] and old["attacks_complete"])
    fields = {key: value for key, value in war.items() if key not in {"sides", "members", "attacks", "aggregate"}}
    if old:
        fields = {key: value for key, value in fields.items() if value is not None}
        if old["type"] != "UNKNOWN" and fields["type"] == "UNKNOWN":
            fields["type"] = old["type"]
        if war["aggregate"]:
            fields["roster_complete"] = old["roster_complete"]
            # A last live snapshot cannot establish complete final attacks.
            fields["attacks_complete"] = 0
    fields.update(last_seen_at=at, content_hash=digest, source_run_id=run)
    if war["result_complete"]:
        fields["final_at"] = old["final_at"] if old and old["final_at"] is not None else at
    if old is None:
        fields["first_seen_at"] = at
        war_id = put(connection, "wars", {}, fields)
        for kind, key in keys:
            if kind == "war_log":
                connection.execute("INSERT INTO war_source_keys VALUES (?,?,?) ON CONFLICT(source_kind,source_key) "
                                   "DO UPDATE SET war_id=excluded.war_id", (kind, key, war_id))
            else:
                connection.execute("INSERT OR IGNORE INTO war_source_keys VALUES (?,?,?)", (kind, key, war_id))
    else:
        put(connection, "wars", {"id": war_id}, fields)
    for side, row in enumerate(war["sides"]):
        previous = connection.execute("SELECT * FROM war_sides WHERE war_id=? AND side=?", (war_id, side)).fetchone()
        values = {key: value for key, value in row.items() if value is not None or previous is None}
        put(connection, "war_sides", {"war_id": war_id, "side": side}, {**values, "source_run_id": run})
    member_ids = {}
    for member in war["members"]:
        row = dict(member)
        side, tag = row.pop("side"), row.pop("player_tag")
        row.update(account_id=_account(connection, tag), source_run_id=run)
        put(connection, "war_members", {"war_id": war_id, "side": side, "player_tag": tag}, row)
    for row in connection.execute("SELECT id,player_tag FROM war_members WHERE war_id=?", (war_id,)):
        tag = row["player_tag"]
        member_ids[tag] = None if tag in member_ids else row["id"]
    complete = war["roster_complete"] and war["attacks_complete"]
    for attack in war["attacks"]:
        row = dict(attack)
        order = row.pop("attack_order")
        attacker, defender = row.pop("attacker"), row.pop("defender")
        row["attacker_member_id"] = member_ids.get(attacker)
        row["defender_member_id"] = member_ids.get(defender)
        extra = json.loads(row["extra_json"])
        if row["attacker_member_id"] is None or row["defender_member_id"] is None:
            extra.update(attacker_tag=attacker, defender_tag=defender)
            row["extra_json"] = canonical_json(extra)
        previous = connection.execute("SELECT * FROM war_attacks WHERE war_id=? AND attack_order=?", (war_id, order)).fetchone()
        if previous:
            previous_extra = json.loads(previous["extra_json"])
            if (any(previous[key] is not None and row[key] != previous[key] for key in ("attacker_member_id", "defender_member_id"))
                    or previous_extra.get("attacker_tag", attacker) != attacker or previous_extra.get("defender_tag", defender) != defender):
                complete = False
                continue
            if not json.loads(previous["extra_json"]).get("stub") and json.loads(row["extra_json"]).get("stub"):
                continue
        put(connection, "war_attacks", {"war_id": war_id, "attack_order": order}, {**row, "source_run_id": run})
    if complete:
        stored_orders = {r[0] for r in connection.execute("SELECT attack_order FROM war_attacks WHERE war_id=?", (war_id,))}
        stored_members = {(r[0], r[1]) for r in connection.execute("SELECT side,player_tag FROM war_members WHERE war_id=?", (war_id,))}
        if stored_orders != {a["attack_order"] for a in war["attacks"]}:
            complete = False
        if stored_members != {(m["side"], m["player_tag"]) for m in war["members"]}:
            complete = False
            connection.execute("UPDATE wars SET roster_complete=0 WHERE id=?", (war_id,))
    if not complete and not war["aggregate"]:
        connection.execute("UPDATE wars SET attacks_complete=0 WHERE id=?", (war_id,))
    return war_id, bool(complete)


def save_group(connection, raw, scope, family_tags, run, at):
    if not isinstance(raw, dict) or raw.get("state") not in {"preparation", "inWar", "ended"}:
        raise ValueError("invalid_group")
    season = raw.get("season")
    if not isinstance(season, str) or len(season) not in (7, 10):
        raise ValueError("invalid_season")
    from datetime import datetime
    try:
        format_string = "%Y-%m" if len(season) == 7 else "%Y-%m-%d"
        parsed = datetime.strptime(season, format_string)
        if parsed.strftime(format_string) != season:
            raise ValueError("invalid_season")
    except ValueError:
        raise ValueError("invalid_season") from None
    season = parsed.strftime("%Y-%m")
    clans, rounds = raw.get("clans"), raw.get("rounds")
    if (not isinstance(clans, list) or len(clans) < 2 or len(clans) % 2 or not isinstance(rounds, list)):
        raise ValueError("incomplete_group")
    projected, tags = [], set()
    for clan in clans:
        side = side_projection_for_group(clan)
        tag = side["clan_tag"]
        if tag in tags or not isinstance(clan.get("members"), list):
            raise ValueError("incomplete_group_clans")
        tags.add(tag)
        players, seen = [], set()
        for member in clan["members"]:
            if not isinstance(member, dict) or not (player_tag := normalize_player_tag(member.get("tag"))) or player_tag in seen:
                raise ValueError("invalid_group_player")
            seen.add(player_tag)
            players.append({"player_tag": player_tag, "name": text(member, "name"),
                            "townhall": optional_int(member, "townHallLevel"), "extra_json": "{}"})
        projected.append((side, players))
    if scope not in tags:
        raise ValueError("wrong_group")
    refs, placeholders, seen_wars = [], [], set()
    for round_no, row in enumerate(rounds, 1):
        if not isinstance(row, dict) or not isinstance(row.get("warTags"), list) or not row["warTags"]:
            raise ValueError("invalid_round")
        for tag in row["warTags"]:
            if tag == "#0":
                placeholders.append(round_no)
                continue
            tag = normalize_player_tag(tag)
            if not tag or tag in seen_wars:
                raise ValueError("invalid_round_tag")
            seen_wars.add(tag)
            refs.append((round_no, tag))
    group_key = ":".join(sorted(tags))
    league = raw.get("league", {})
    if not isinstance(league, dict):
        raise ValueError("invalid_group_league")
    league_id = optional_int(league, "id") if "league" in raw else optional_int(raw, "leagueId")
    league_name = text(league, "name") if "league" in raw else text(raw, "leagueName")
    season_id = put(connection, "cwl_seasons", {"season_key": season, "group_key": group_key}, {
        "league_id": league_id, "league_name": league_name,
        "state": raw["state"], "group_metadata_json": canonical_json({"round_count": len(rounds),
            "expected_round_count": len(tags) - 1, "wars_per_round": len(tags) // 2,
            "placeholder_rounds": sorted(set(placeholders)), "field_presence": sorted(raw.keys() & {
                "state", "season", "clans", "rounds", "league", "leagueId", "leagueName"})}), "source_run_id": run,
        **({"first_seen_at": at} if not connection.execute(
            "SELECT 1 FROM cwl_seasons WHERE season_key=? AND group_key=?", (season, group_key)).fetchone() else {})})
    for side, players in projected:
        tag = side["clan_tag"]
        if tag in family_tags:
            connection.execute("INSERT OR IGNORE INTO cwl_season_sources VALUES (?,?)", (season_id, tag))
        put(connection, "cwl_clans", {"season_id": season_id, "clan_tag": tag}, {
            **{key: value for key, value in side.items() if key != "clan_tag"}, "source_run_id": run})
        for member in players:
            player_tag = member.pop("player_tag")
            put(connection, "cwl_players", {"season_id": season_id, "clan_tag": tag, "player_tag": player_tag},
                {**member, "account_id": _account(connection, player_tag), "source_run_id": run})
    for round_no, tag in refs:
        connection.execute("INSERT OR IGNORE INTO cwl_rounds(season_id,round_no,war_tag,source_run_id) VALUES (?,?,?,?)",
                           (season_id, round_no, tag, run))
        connection.execute("INSERT OR IGNORE INTO war_collection_state(source_kind,scope_id,next_due_at) VALUES ('cwl_war',?,?)",
                           (tag, at))
    return season_id


def side_projection_for_group(raw):
    if not isinstance(raw, dict) or not (tag := normalize_player_tag(raw.get("tag"))):
        raise ValueError("invalid_group_clan")
    return {"clan_tag": tag, "name": text(raw, "name"), "level": optional_int(raw, "clanLevel"),
            "badge_json": badge(raw), "extra_json": "{}"}


def finish_seasons(connection, at):
    for row in connection.execute("SELECT * FROM cwl_seasons WHERE state='ended' AND final_at IS NULL").fetchall():
        metadata = json.loads(row["group_metadata_json"])
        if metadata["placeholder_rounds"] or metadata["round_count"] != metadata["expected_round_count"]:
            continue
        rounds = connection.execute(
            "SELECT round_no,COUNT(*) FROM cwl_rounds WHERE season_id=? GROUP BY round_no ORDER BY round_no",
            (row["id"],)).fetchall()
        if [(r[0], r[1]) for r in rounds] != [(n, metadata["wars_per_round"])
                                             for n in range(1, metadata["expected_round_count"] + 1)]:
            continue
        pending = connection.execute(
            "SELECT COUNT(*) FROM cwl_rounds r LEFT JOIN war_collection_state s "
            "ON s.source_kind='cwl_war' AND s.scope_id=r.war_tag WHERE r.season_id=? "
            "AND (s.enabled IS NULL OR s.enabled=1)", (row["id"],)).fetchone()[0]
        if not pending:
            connection.execute("UPDATE cwl_seasons SET final_at=? WHERE id=?", (at, row["id"]))


def record_response(connection, run, ordinal, kind, scope, response, family_tags, at):
    """One validated response and its checkpoint commit atomically.

    Changed facts reference the run. Only a failed item keeps its own receipt.
    """
    with sqlite_transaction(connection, immediate=True):
        state = war_state.queued(connection, run, ordinal, kind, scope)
        checkpoint = json.loads(state["checkpoint_json"])
        before = connection.total_changes
        connection.execute("SAVEPOINT response_facts")
        interval = 86400 if kind == "war_log" else 60 if kind == "cwl_war" else 300
        due, war_id, enabled = next_slot(at, interval), state["war_id"], state["enabled"]
        status, error, count, complete = "ok", None, None, True
        rolled_back = False

        def recovery(previous, *, force=False):
            if previous and (force or checkpoint.get("recovery_war_id") != previous):
                recover_log(connection, scope, previous, at)
                checkpoint["recovery_war_id"] = previous

        try:
            if kind == "cwl_group" and response is not None and response.error is None and (response.status == 404 or
                    response.ok and isinstance(response.payload, dict) and response.payload.get("state") == "notInWar"):
                status = "empty"
            elif response is None or not response.ok or response.error is not None or not isinstance(response.payload, dict):
                raise ValueError("fetch_failed")
            elif kind == "cwl_group":
                save_group(connection, response.payload, scope, family_tags, run, at)
            elif kind == "war_log":
                count, checkpoint, more = save_log(connection, response.payload, scope, checkpoint, run, at)
                due = at if more else due
                status = "ok" if count else "empty"
            elif kind == "current_war" and response.payload.get("state") == "notInWar":
                status = "empty"
                recovery(war_id, force=not checkpoint.get("current_empty", False))
                checkpoint["current_empty"] = True
            else:
                war = project_war(response.payload, war_tag=scope if kind == "cwl_war" else None)
                tags = {side["clan_tag"] for side in war["sides"]}
                if kind == "current_war" and scope not in tags:
                    raise ValueError("wrong_clan")
                if kind == "cwl_war" and not tags.intersection(family_tags):
                    # A first discovery GET is unavoidable: group rounds expose
                    # tags, not the pairings. Never poll an unrelated war again.
                    enabled = 0
                    checkpoint = {"unrelated": True, "clan_tags": sorted(tags)}
                else:
                    old_id = war_id
                    war_id, complete = save_war(connection, war, run, at)
                    if kind == "cwl_war":
                        connection.execute("UPDATE cwl_rounds SET war_id=? WHERE war_tag=? AND war_id IS NULL", (war_id, scope))
                        row = connection.execute("SELECT * FROM wars WHERE id=?", (war_id,)).fetchone()
                        if sealed(row):
                            enabled = 0
                        elif war["state"] != "warEnded":
                            due = (at + 180 * SECOND if at >= war["end_at"] + 90 * SECOND else
                                   min(due, war["end_at"] + 90 * SECOND))
                    else:
                        checkpoint.pop("current_empty", None)
                        if old_id and old_id != war_id:
                            recovery(old_id, force=True)
                        if war["state"] != "warEnded":
                            end = war["end_at"]
                            if at >= end + 90 * SECOND:
                                due = at + 180 * SECOND
                                recovery(war_id)
                            elif at >= end - 300 * SECOND:
                                due = min(at + 60 * SECOND, end + 90 * SECOND)
                            else:
                                due = min(due, end - 300 * SECOND)
                    count = len(war["attacks"])
                    complete = complete and (war["state"] != "warEnded" or war["result_complete"])
                    if not complete:
                        status, error = "failed", "partial_war"
                        if kind == "current_war" and war["state"] == "warEnded":
                            due = at + 180 * SECOND
            if response is not None and response.ok and response.error is None:
                collect_fields(connection, kind, scope, response.payload, run, at,
                               complete=complete and kind != "war_log")
        except (ValueError, TypeError, OverflowError) as exc:
            connection.execute("ROLLBACK TO response_facts")
            rolled_back = True
            due, war_id, enabled, checkpoint = (next_slot(at, interval), state["war_id"], state["enabled"],
                                               json.loads(state["checkpoint_json"]))
            previous = connection.execute("SELECT * FROM wars WHERE id=?", (war_id,)).fetchone() if war_id else None
            if previous and previous["final_at"] is None and kind in {"current_war", "cwl_war"}:
                end = previous["end_at"]
                if at >= end + 90 * SECOND:
                    due = at + 180 * SECOND
                elif at >= end - 300 * SECOND:
                    due = min(at + 60 * SECOND, end + 90 * SECOND)
            status, error, complete = "failed", str(exc) if isinstance(exc, ValueError) else "invalid_payload", False
        connection.execute("RELEASE response_facts")
        written = 0 if rolled_back else connection.total_changes - before
        connection.execute(
            "UPDATE war_collection_state SET next_due_at=?,war_id=?,enabled=?,checkpoint_json=? WHERE source_kind=? AND scope_id=?",
            (due, war_id, enabled, canonical_json(checkpoint), kind, scope))
        war_state.outcome(connection, run, state, status, at, error=error, response=response,
                          count=count, written=written, complete=complete)
        finish_seasons(connection, at)


def recover_log(connection, scope, war_id, at):
    old = connection.execute("SELECT * FROM wars WHERE id=?", (war_id,)).fetchone() if war_id else None
    if old and old["final_at"] is None:
        connection.execute("UPDATE war_collection_state SET next_due_at=MIN(next_due_at,?) WHERE source_kind='war_log' AND scope_id=?",
                           (at, scope))


def save_log(connection, raw, scope, checkpoint, run, at):
    items = raw.get("items")
    paging = raw.get("paging", {})
    if not isinstance(items, list) or not isinstance(paging, dict) or not isinstance(paging.get("cursors", {}), dict):
        raise ValueError("invalid_war_log")
    after = paging.get("cursors", {}).get("after")
    if after is not None and (not isinstance(after, str) or not after or after == checkpoint.get("after")):
        raise ValueError("invalid_war_log_cursor")
    projected = [project_log_entry(entry) for entry in items]
    if any(scope not in ({war["clan_tag"]} if war.get("cwl_summary") else
                         {s["clan_tag"] for s in war["sides"]}) for war in projected):
        raise ValueError("wrong_war_log_clan")
    frontier = checkpoint.get("frontier")
    reached = False
    newest = checkpoint.get("newest")
    for war in projected:
        identity = war["clan_tag"] if war.get("cwl_summary") else ':'.join(s['clan_tag'] for s in war['sides'])
        key = f"{identity}:{war['end_at']}"
        newest = newest or key
        if key == frontier:
            reached = True
        if war.get("cwl_summary"):
            put(connection, "cwl_log_summaries", {"clan_tag": war["clan_tag"], "end_at": war["end_at"]},
                {**{k: v for k, v in war.items() if k not in {"cwl_summary", "clan_tag", "end_at"}}, "source_run_id": run})
        else:
            save_war(connection, war, run, at)
    if after and not reached:
        return len(items), {"frontier": frontier, "newest": newest, "after": after}, True
    return len(items), {"frontier": newest or frontier, "history_complete": not frontier or reached,
                        "api_history_limit": bool(frontier and not reached)}, False

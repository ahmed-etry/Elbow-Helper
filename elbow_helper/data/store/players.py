"""Change-only player facts and hourly counter samples, with fetch provenance."""

from datetime import datetime, timezone
import json

from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from . import FamilyStore
from .people import account_name
from .player_projection import ARRAYS, COUNTERS, HOUR_US, TRANSIENT, project_profile
from . import player_state
from .projections import canonical_json, nonnegative_int


def due_accounts(connection, at):
    """Reuse complete clan membership and shadow links; never fetch clans here."""
    rows = connection.execute(
        "SELECT a.id,a.player_tag FROM accounts a WHERE EXISTS ("
        "SELECT 1 FROM clan_membership m JOIN clans c ON c.id=m.clan_id "
        "WHERE m.account_id=a.id AND m.to_at IS NULL AND c.is_family=1) OR EXISTS ("
        "SELECT 1 FROM account_links l WHERE l.account_id=a.id AND l.to_at IS NULL) ORDER BY a.id",
    ).fetchall()
    due = []
    for row in rows:
        next_due = connection.execute("SELECT next_due_at FROM player_collection_state WHERE account_id=?",
                                      (row["id"],)).fetchone()
        if next_due is None or next_due[0] is None or at >= next_due[0]:
            due.append(dict(row))
    return due


def players_due(connection, at):
    return FamilyStore._due(connection, "players", 3600, at) or bool(due_accounts(connection, at))


def _previous(connection, account_id, section):
    row = connection.execute("SELECT last_good_json FROM player_collection_state WHERE account_id=?", (account_id,)).fetchone()
    value = json.loads(row[0]) if row else {}
    for key in section.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _catalog(connection, source, row, classification=None):
    connection.execute(
        "INSERT INTO item_catalog(source_array,village,source_name,source_identifier,classification,definition_json) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(source_array,village,source_name) DO UPDATE SET "
        "classification=excluded.classification WHERE excluded.classification IS NOT NULL "
        "AND excluded.classification IS NOT item_catalog.classification",
        (source, row.get("village", ""), row["name"], str(row["id"]) if "id" in row else None,
         classification if classification is not None else ARRAYS[source], canonical_json({"projection_version": 1})),
    )
    return connection.execute(
        "SELECT id FROM item_catalog WHERE source_array=? AND village=? AND source_name=?",
        (source, row.get("village", ""), row["name"]),
    ).fetchone()[0]


def _fields(connection, account_id, fields, at, item):
    old = {row["field_key"]: row["value_json"] for row in connection.execute(
        "SELECT p.* FROM player_observations p WHERE account_id=? AND observed_at=("
        "SELECT MAX(observed_at) FROM player_observations WHERE account_id=p.account_id AND field_key=p.field_key)",
        (account_id,),
    )}
    previous = _previous(connection, account_id, "fields")
    for key in old.keys() | fields.keys():
        value = canonical_json(fields.get(key))
        if old.get(key) != value:
            connection.execute("INSERT INTO player_observations VALUES (?,?,?,?,?,?)",
                               (account_id, key, at, value, previous, item))


def _items(connection, account_id, profile, at, item, *, store=True):
    old = {row["item_id"]: row for row in connection.execute(
        "SELECT p.*,c.source_array FROM player_items p JOIN item_catalog c ON c.id=p.item_id "
        "WHERE p.account_id=? AND p.observed_at=(SELECT MAX(observed_at) FROM player_items "
        "WHERE account_id=p.account_id AND item_id=p.item_id)", (account_id,),
    )} if store else {}
    achievements, transient, identities = {}, {}, {}
    pet_names = {(row.get("village", ""), row["name"]) for row in profile.items["pets"]}
    for source, rows in profile.items.items():
        seen = set()
        previous = _previous(connection, account_id, f"arrays.{source}")
        for row in rows:
            # Raw troops carry no reliable pet discriminator. An explicit API
            # pet array/name or discriminator is evidence; an unknown stays unknown.
            classification = ("pet" if source == "troops" and (row.get("isPet") is True
                              or (row.get("village", ""), row["name"]) in pet_names) else None)
            item_id = _catalog(connection, source, row, classification)
            identities[(source, row.get("village", ""), row["name"])] = item_id
            seen.add(item_id)
            static = canonical_json({key: value for key, value in row.items()
                                     if key not in {"name", "village", "id", "level", "maxLevel", "value", *TRANSIENT}})
            values = (row.get("level"), row.get("maxLevel"), static, 1)
            last = old.get(item_id)
            if store and (last is None or tuple(last[key] for key in ("level", "max_level", "static_fields_json", "present")) != values):
                connection.execute("INSERT INTO player_items VALUES (?,?,?,?,?,?,?,?,?)",
                                   (account_id, item_id, at, *values, previous, item))
            if source == "achievements" and "value" in row:
                achievements[str(item_id)] = row["value"]
            fast = {key: value for key, value in row.items() if key in TRANSIENT - {"equipment"}}
            if fast:
                transient[str(item_id)] = fast
        if store and profile.complete_arrays.get(source):
            for item_id, last in old.items():
                if last["source_array"] == source and last["present"] and item_id not in seen:
                    connection.execute("INSERT INTO player_items VALUES (?,?,?,NULL,NULL,'{}',0,?,?)",
                                       (account_id, item_id, at, previous, item))
    return identities, achievements, transient


def _equipment(connection, account_id, profile, identities, at, item, *, store=True):
    complete = True
    observed = {}
    heroes = {identities[("heroes", row.get("village", ""), row["name"])]: row
              for row in profile.items["heroes"]}
    old = {}
    for row in connection.execute(
        "SELECT e.* FROM hero_equipment e WHERE account_id=? AND observed_at=(SELECT MAX(observed_at) "
        "FROM hero_equipment WHERE account_id=e.account_id AND hero_item_id=e.hero_item_id AND slot=e.slot)",
        (account_id,),
    ):
        old.setdefault(row["hero_item_id"], {})[row["slot"]] = row["equipment_item_id"]
    for hero in heroes.keys() | old.keys():
        observed[str(hero)] = False
        previous = _previous(connection, account_id, f"equipment.{hero}")
        row = heroes.get(hero)
        if row is None:
            if not profile.complete_arrays.get("heroes"):
                continue
            equipped = []
        else:
            equipped = row.get("equipment")
        if not isinstance(equipped, list):
            # Missing nested arrays cannot unequip a hero.
            if row is not None and "equipment" in row:
                complete = False
            continue
        slots = {}
        valid = True
        for index, entry in enumerate(equipped):
            slot = entry.get("slot", index) if isinstance(entry, dict) else None
            if (not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"]
                    or not nonnegative_int(slot) or slot in slots
                    or not isinstance(entry.get("village", ""), str)):
                valid = False
                break
            # Use the top-level equipment's API identity when village is omitted
            # in a reference, provided the name is unambiguous.
            matches = [identity for (source, village, name), identity in identities.items()
                       if source == "heroEquipment" and name == entry["name"]
                       and ("village" not in entry or village == entry["village"])]
            if len(matches) != 1:
                valid = False
                break
            slots[slot] = matches[0]
        if not valid:
            complete = False
            continue
        observed[str(hero)] = True
        for slot in slots.keys() | old.get(hero, {}).keys():
            value = slots.get(slot)
            if store and old.get(hero, {}).get(slot) != value:
                connection.execute("INSERT INTO hero_equipment VALUES (?,?,?,?,?,?,?)",
                                   (account_id, hero, slot, at, value, previous, item))
    return complete, observed


def _counters(connection, account_id, profile, achievements, transient, at, run, state):
    last = connection.execute(
        "SELECT * FROM player_counters WHERE account_id=? ORDER BY sampled_at DESC LIMIT 1", (account_id,),
    ).fetchone()
    if last and at // HOUR_US <= last["sampled_at"] // HOUR_US:
        return
    # Progress is a daily sparse patch. The full current map is only a bounded
    # checkpoint, never copied into every hourly history row.
    daily = state["progress_sampled_at"] is None or at // (24 * HOUR_US) > state["progress_sampled_at"] // (24 * HOUR_US)
    old_achievements = json.loads(state["achievement_values_json"])
    current = (dict(achievements) if profile.complete_arrays.get("achievements") else
               {**old_achievements, **achievements})
    diff = {key: current.get(key) for key in old_achievements.keys() | current.keys()
            if key not in old_achievements or key not in current or old_achievements[key] != current[key]} if daily else {}
    if daily:
        connection.execute("UPDATE player_collection_state SET progress_sampled_at=?,achievement_values_json=? WHERE account_id=?",
                           (at, canonical_json(current), account_id))
    presence = sum(1 << index for index, key in enumerate(COUNTERS) if key in profile.counters)
    present_json = canonical_json({"fields": presence, "legendStatistics": profile.legend is not None})
    legend_json = canonical_json(profile.legend) if profile.legend is not None else None
    transient_json = canonical_json(transient)
    values = [profile.counters.get(key) for key in COUNTERS]
    fast_changed = (last is None or values != [last[column] for column in COUNTERS.values()]
                    or last["field_presence_json"] != present_json or last["legend_counters_json"] != legend_json
                    or last["transient_item_fields_json"] != transient_json)
    if not fast_changed and not diff:
        return
    decreased = [key for key, column in COUNTERS.items() if last and key in profile.counters
                 and last[column] is not None and profile.counters[key] < last[column]]
    decreased_achievements = [key for key, value in current.items() if daily
                             if key in old_achievements and value < old_achievements[key]]
    current_context = {"utc_month": datetime.fromtimestamp(at / 1_000_000, timezone.utc).strftime("%Y-%m"),
                       "clan": profile.fields.get("clan", {}).get("tag")}
    old_context = json.loads(state["counter_context_json"])
    context = {key: value for key, value in current_context.items() if key not in old_context or value != old_context[key]}
    if decreased:
        context["decreased_fields"] = decreased
    if decreased_achievements:
        context["decreased_achievements"] = decreased_achievements
    columns = list(COUNTERS.values())
    connection.execute(
        f"INSERT INTO player_counters(account_id,sampled_at,{','.join(columns)},legend_counters_json,"
        "achievement_values_json,transient_item_fields_json,field_presence_json,reset_context_json,source_run_id) "
        f"VALUES ({','.join('?' for _ in range(len(values) + 8))})",
        (account_id, at, *values, legend_json, canonical_json(diff), transient_json, present_json, canonical_json(context), run),
    )
    connection.execute("UPDATE player_collection_state SET counter_context_json=? WHERE account_id=?",
                       (canonical_json(current_context), account_id))


def collect_player(connection, run, ordinal, account_id, tag, response, at):
    with sqlite_transaction(connection, immediate=True):
        state = connection.execute("SELECT * FROM player_collection_state WHERE account_id=? AND active_run_id=? AND ordinal=?",
                                   (account_id, run, ordinal)).fetchone()
        if state is None:
            raise ValueError("profile_not_queued")
        payload = response.payload if response and response.ok and response.error is None else None
        try:
            profile = project_profile(payload, tag)
        except (ValueError, TypeError, OverflowError):
            error = ("profile_missing" if response and response.status == 404 else "invalid_profile"
                     if payload is not None else "transport_error" if response and response.error else "fetch_failed")
            item = player_state.evidence(connection, run, ordinal, account_id, tag, state["started_at"])
            FamilyStore.outcome(connection, item, "failed", at, error=error, response=response, complete=False)
            player_state.outcome(connection, run, account_id, at, "failed", 0, missing=bool(response and response.status == 404))
            return
        account = connection.execute("SELECT last_profile_at FROM accounts WHERE id=?", (account_id,)).fetchone()
        if account[0] is not None and at < account[0]:
            raise ValueError("out_of_order_observation")
        before = connection.total_changes
        daily = state["slow_sampled_at"] is None or at // (24 * HOUR_US) > state["slow_sampled_at"] // (24 * HOUR_US)
        if daily:
            _fields(connection, account_id, profile.fields, at, run)
        identities, achievements, transient = _items(connection, account_id, profile, at, run, store=daily)
        equipment_complete, equipment_observed = _equipment(connection, account_id, profile, identities, at, run, store=daily)
        _counters(connection, account_id, profile, achievements, transient, at, run, state)
        alias = connection.execute("SELECT name FROM account_aliases WHERE account_id=? AND to_at IS NULL", (account_id,)).fetchone()
        item = None
        if alias is None or alias[0] != profile.fields["name"]:
            # Names also feed Part A's shared alias intervals, whose provenance
            # contract still needs a collection item. Keep one only on a rename.
            item = player_state.evidence(connection, run, ordinal, account_id, tag, state["started_at"])
            account_name(connection, account_id, profile.fields["name"], at, item)
        connection.execute("UPDATE accounts SET last_profile_at=? WHERE id=?", (at, account_id))
        complete = profile.complete and equipment_complete
        completeness = {
            "complete": complete, "fields": True, "arrays": profile.complete_arrays,
            "equipment": equipment_observed, "unsupported_fields": profile.unsupported,
        }
        written = connection.total_changes - before
        if not complete and item is None:
            item = player_state.evidence(connection, run, ordinal, account_id, tag, state["started_at"])
        if item is not None:
            FamilyStore.outcome(connection, item, "ok" if complete else "failed", at, response=response,
                                error=None if complete else "partial_profile", count=sum(map(len, profile.items.values())),
                                written=written, complete=complete)
            connection.execute("UPDATE collection_items SET completeness_json=? WHERE id=?", (canonical_json(completeness), item))
        if daily:
            last_good = json.loads(state["last_good_json"])
            last_good["fields"] = at
            for section, values in (("arrays", profile.complete_arrays), ("equipment", equipment_observed)):
                last_good.setdefault(section, {}).update({key: at for key, valid in values.items() if valid})
            connection.execute("UPDATE player_collection_state SET slow_sampled_at=?,last_good_json=? WHERE account_id=?",
                               (at, canonical_json(last_good), account_id))
        player_state.coverage(connection, account_id, run, at, completeness)
        player_state.outcome(connection, run, account_id, at, "ok" if complete else "partial", written)

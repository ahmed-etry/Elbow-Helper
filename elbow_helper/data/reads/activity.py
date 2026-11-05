"""Each account's progress as of a moment and its donations in a window, as clan health reads them.

Progress carries the old health rule for level sums: home heroes, home troops
split into pets by name, all hero equipment and home spells, from the record's
item levels; lifetime Clan Games points from the latest "Games Champion"
achievement value. Before the record's own items for an account, its copied old
totals stand. Donations follow the clan roster's 15-minute samples; before an
account's first one, its profile counters (the copied old ones included). A
fall in a counter is a season reset: the new value counts in full. Never asks Clash.
"""

import json

from ..store.player_projection import COUNTERS
from ..store.profile_schema import TOTALS
from .core import RecordReads, tags as checked_tags, timestamp


def unit_name_key(name):
    return "".join(char for char in str(name or "").casefold() if char.isalnum())


# Supercell's Pet List, checked 2026-10-27:
# https://support.supercell.com/clash-of-clans/en/articles/about-heroes-pets-9.html
PET_NAME_KEYS = frozenset(unit_name_key(name) for name in (
    "L.A.S.S.I", "Electro Owl", "Mighty Yak", "Unicorn", "Frosty", "Diggy",
    "Poison Lizard", "Phoenix", "Spirit Fox", "Angry Jelly", "Sneezy", "Greedy Raven"))
# Profile counters a health row uses, by its own name.
PROGRESS_COUNTERS = {"capital_contrib": "clanCapitalContributions", "war_stars": "warStars",
                     "attack_wins": "attackWins"}
GAMES = "games champion"


def level_sums(items):
    """Old health's level sums from (source array, village, name, level) rows."""
    sums = dict.fromkeys(("hero_sum", "pet_sum", "equipment_sum", "troop_sum", "spell_sum"), 0)
    for source, village, name, level in items:
        if type(level) is not int:
            continue
        village = str(village or "").lower()
        if source == "heroes" and village in ("", "home"):
            sums["hero_sum"] += level
        elif source == "troops" and village == "home":
            sums["pet_sum" if unit_name_key(name) in PET_NAME_KEYS else "troop_sum"] += level
        elif source == "heroEquipment":
            sums["equipment_sum"] += level
        elif source == "spells" and village in ("", "home"):
            sums["spell_sum"] += level
    return sums


def counter_at(connection, account, key, at, until):
    """The field's last sample at or before ``at``; failing that, its first one up to ``until``."""
    column, bit = COUNTERS[key], 1 << list(COUNTERS).index(key)
    present = ("((json_type(field_presence_json,'$.fields')='integer' AND "
               "(json_extract(field_presence_json,'$.fields') & ?) != 0) OR "
               "EXISTS (SELECT 1 FROM json_each(field_presence_json,'$.fields') WHERE value=?))")
    row = connection.execute(f"SELECT {column},sampled_at FROM player_counters WHERE account_id=? AND sampled_at<=? "
                             f"AND {present} ORDER BY sampled_at DESC LIMIT 1", (account, at, bit, key)).fetchone()
    if row is None and until is not None:
        row = connection.execute(f"SELECT {column},sampled_at FROM player_counters WHERE account_id=? AND sampled_at>? "
                                 f"AND sampled_at<=? AND {present} ORDER BY sampled_at LIMIT 1",
                                 (account, at, until, bit, key)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def items_at(connection, account, at):
    rows = connection.execute(
        "SELECT c.source_array,c.village,c.source_name,p.level,p.present FROM player_items p "
        "JOIN item_catalog c ON c.id=p.item_id WHERE p.account_id=? "
        "AND c.source_array IN ('heroes','troops','spells','heroEquipment') AND p.observed_at=("
        "SELECT MAX(observed_at) FROM player_items WHERE account_id=p.account_id AND item_id=p.item_id "
        "AND observed_at<=?)", (account, at)).fetchall()
    return [row[:4] for row in rows if row[4]]


def games_at(connection, account, ids, at):
    """The newest Games Champion value at or before ``at`` and when; diffs name a dropped item with null."""
    best = (None, None)
    for item in ids:
        row = connection.execute(
            "SELECT json_extract(achievement_values_json,'$.\"' || ? || '\"'),sampled_at FROM player_counters "
            "WHERE account_id=? AND sampled_at<=? AND json_type(achievement_values_json,'$.\"' || ? || '\"') "
            "IS NOT NULL ORDER BY sampled_at DESC LIMIT 1", (str(item), account, at, str(item))).fetchone()
        if row and row[0] is not None and (best[1] is None or row[1] > best[1]):
            best = (row[0], row[1])
    return best


def old_total(connection, account, key, at, until):
    row = connection.execute(f"SELECT {key},observed_at FROM old_profile_totals WHERE account_id=? AND observed_at<=? "
                             f"AND {key} IS NOT NULL ORDER BY observed_at DESC LIMIT 1", (account, at)).fetchone()
    if row is None and until is not None:
        row = connection.execute(f"SELECT {key},observed_at FROM old_profile_totals WHERE account_id=? AND observed_at>? "
                                 f"AND observed_at<=? AND {key} IS NOT NULL ORDER BY observed_at LIMIT 1",
                                 (account, at, until)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def progress_row(connection, tag, at, until, games_ids):
    values = {"player_tag": tag, "townhall": None, "trophies": None, **dict.fromkeys(TOTALS), **dict.fromkeys(PROGRESS_COUNTERS)}
    observed, sources = {}, {}
    account = connection.execute("SELECT id FROM accounts WHERE player_tag=?", (tag,)).fetchone()
    if account is None:
        return {**values, "known": False, "observed_at": observed, "sources": sources}
    account = account[0]
    for name, key in PROGRESS_COUNTERS.items():
        values[name], when = counter_at(connection, account, key, at, until)
        if when is not None:
            observed[name], sources[name] = when, "record"
    row = connection.execute("SELECT value_json,observed_at FROM player_observations WHERE account_id=? "
                             "AND field_key='townHallLevel' AND observed_at<=? ORDER BY observed_at DESC LIMIT 1",
                             (account, at)).fetchone()
    if row is None and until is not None:
        row = connection.execute("SELECT value_json,observed_at FROM player_observations WHERE account_id=? "
                                 "AND field_key='townHallLevel' AND observed_at>? AND observed_at<=? "
                                 "ORDER BY observed_at LIMIT 1", (account, at, until)).fetchone()
    if row:
        values["townhall"], observed["townhall"], sources["townhall"] = json.loads(row[0]), row[1], "record"
    # Trophies as the clan roster showed them; profile counters read 0 for many accounts.
    row = connection.execute("SELECT trophies,sampled_at FROM donation_snapshots WHERE account_id=? AND sampled_at<=? "
                             "AND trophies IS NOT NULL ORDER BY sampled_at DESC LIMIT 1", (account, at)).fetchone()
    values["trophies"] = row[0] if row else None
    if row:
        observed["trophies"], sources["trophies"] = row[1], "clan_roster"
    # The record's item levels from its first item day on; the copied old totals before it. Only a total
    # with neither at the moment takes its first value up to ``until``, from the record or the old copy.
    first = connection.execute("SELECT MIN(observed_at) FROM player_items WHERE account_id=?", (account,)).fetchone()[0]
    if first is not None and first <= at:
        for name, value in level_sums(items_at(connection, account, at)).items():
            values[name], observed[name], sources[name] = value, at, "record"
    games, when = games_at(connection, account, games_ids, at)
    if games is not None:
        values["games_total"], observed["games_total"], sources["games_total"] = games, when, "record"
    for name in TOTALS:
        if values[name] is None:
            value, when = old_total(connection, account, name, at, None)
            if value is not None:
                values[name], observed[name], sources[name] = value, when, "legacy_health"
    if until is not None and any(values[name] is None for name in TOTALS):
        later = {}
        if first is not None and at < first <= until:
            later.update({name: (value, first) for name, value in level_sums(items_at(connection, account, first)).items()})
        sample = connection.execute("SELECT MIN(sampled_at) FROM player_counters WHERE account_id=? AND sampled_at>? "
                                    "AND sampled_at<=? AND achievement_values_json!='{}'", (account, at, until)).fetchone()[0]
        if sample is not None:
            games, when = games_at(connection, account, games_ids, sample)
            if games is not None:
                later["games_total"] = (games, when)
        for name in TOTALS:
            if values[name] is not None:
                continue
            old, old_when = old_total(connection, account, name, at, until)
            record_value, record_when = later.get(name, (None, None))
            # Whichever source saw it first inside the window.
            if old is not None and (record_when is None or old_when < record_when):
                values[name], observed[name], sources[name] = old, old_when, "legacy_health"
            elif record_value is not None:
                values[name], observed[name], sources[name] = record_value, record_when, "record"
    return {**values, "known": True, "observed_at": observed, "sources": sources}


def progress(connection, selected, at, until):
    ids = [row[0] for row in connection.execute(
        "SELECT id FROM item_catalog WHERE source_array='achievements' AND lower(trim(source_name))=?", (GAMES,))]
    return {"as_of": at, "until": until, "rows": [progress_row(connection, tag, at, until, ids) for tag in selected]}


def donation_points(connection, account, before):
    """(time, donations, received) samples up to ``before``: roster samples, profile counters before them."""
    rows = connection.execute("SELECT sampled_at,donations,received FROM donation_snapshots WHERE account_id=? "
                              "AND sampled_at<? AND donations IS NOT NULL AND received IS NOT NULL ORDER BY sampled_at",
                              (account, before)).fetchall()
    first = connection.execute("SELECT MIN(sampled_at) FROM donation_snapshots WHERE account_id=? "
                               "AND donations IS NOT NULL", (account,)).fetchone()[0]
    end = before if first is None else min(first, before)
    bits = (1 << list(COUNTERS).index("donations")) | (1 << list(COUNTERS).index("donationsReceived"))
    earlier = connection.execute(
        "SELECT sampled_at,donations,received FROM player_counters WHERE account_id=? AND sampled_at<? "
        "AND donations IS NOT NULL AND received IS NOT NULL AND ((json_type(field_presence_json,'$.fields')='integer' "
        "AND (json_extract(field_presence_json,'$.fields') & ?)=?) OR (SELECT COUNT(*) FROM "
        "json_each(field_presence_json,'$.fields') WHERE value IN ('donations','donationsReceived'))=2) "
        "ORDER BY sampled_at", (account, end, bits, bits)).fetchall()
    return [tuple(row) for row in (*earlier, *rows)]


def donations(connection, selected, after, before):
    """Donations and received in (after, before), reset-aware, from the last sample at or before ``after``."""
    result = []
    for tag in selected:
        account = connection.execute("SELECT id FROM accounts WHERE player_tag=?", (tag,)).fetchone()
        totals = {"player_tag": tag, "donations": 0, "donations_received": 0, "samples": 0}
        previous = None
        for at, given, received in donation_points(connection, account[0], before) if account else ():
            if at <= after:
                previous = (given, received)
                continue
            totals["samples"] += 1
            for index, name in ((0, "donations"), (1, "donations_received")):
                value = (given, received)[index]
                last = None if previous is None else previous[index]
                totals[name] += value if last is None or value < last else value - last
            previous = (given, received)
        result.append(totals)
    return {"after": after, "before": before, "rows": result}


class ActivityReads(RecordReads):
    """Progress and donations for clan health, from the family record."""

    async def progress(self, player_tags, *, as_of, until=None):
        """Each account's progress at ``as_of``; a field with nothing by then takes its first value up to ``until``."""
        at = timestamp(as_of)
        if until is not None and timestamp(until) < at:
            raise ValueError("Expected until at or after as_of")
        return await self._read(progress, checked_tags(player_tags), at, until)

    async def donations(self, player_tags, *, after, before):
        after, before = timestamp(after), timestamp(before)
        if before <= after:
            raise ValueError("Expected a window that ends after it starts")
        return await self._read(donations, checked_tags(player_tags), after, before)

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
from .core import RecordReads, tags as checked_tags, timestamp, placeholders
from .coverage import CADENCES


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


DONATION_FIELDS = ("donations", "donations_received")
# Every stored sample lies before this; SQLite's largest integer.
UNBOUNDED = (1 << 63) - 1


def counted(points, after, before):
    """Totals in (after, before) from samples oldest first, from the last sample at or before ``after``.

    Also returns the time of each sample that fell.
    """
    totals = {"donations": 0, "donations_received": 0, "samples": 0}
    previous, falls = None, []
    for at, given, received in points:
        if at <= after:
            previous = (given, received)
            continue
        if at >= before:
            break
        totals["samples"] += 1
        fell = False
        for index, name in enumerate(DONATION_FIELDS):
            value = (given, received)[index]
            last = None if previous is None else previous[index]
            fell = fell or last is not None and value < last
            totals[name] += value if last is None or value < last else value - last
        if fell:
            falls.append(at)
        previous = (given, received)
    return totals, falls


def donations(connection, selected, after, before):
    """Donations and received in (after, before), reset-aware, from the last sample at or before ``after``."""
    result = []
    for tag in selected:
        account = connection.execute("SELECT id FROM accounts WHERE player_tag=?", (tag,)).fetchone()
        totals, _ = counted(donation_points(connection, account[0], before) if account else (), after, before)
        result.append({"player_tag": tag, **totals})
    return {"after": after, "before": before, "rows": result}


def latest_donation_samples(connection, selected, before):
    """The last roster sample at or before a moment; keep accounts without samples."""
    rows = []
    for tag in selected:
        sample = connection.execute(
            'SELECT d.donations,d.received,d.sampled_at,c.family_code FROM donation_snapshots d '
            'JOIN accounts a ON a.id=d.account_id LEFT JOIN clans c ON c.id=d.clan_id '
            'WHERE a.player_tag=? AND d.sampled_at<=? ORDER BY d.sampled_at DESC LIMIT 1',
            (tag, before)).fetchone()
        rows.append({'player_tag': tag, **dict(zip(
            ('donations', 'donations_received', 'sampled_at', 'clan_code'),
            sample if sample is not None else (None,) * 4))})
    return {'before': before, 'rows': rows}


# A stretch is polled on schedule while polls are at most two cadences apart, as source coverage counts
# a missed observation; the hourly profile cadence is the coarsest of the collectors read here.
PROFILE_GAP = 2 * CADENCES["player_profile"]
ROSTER_GAP = 2 * CADENCES["clan_roster"]


def runs(points, gap):
    """[first, last] stretches of sorted poll times no more than ``gap`` apart."""
    stretches = []
    for point in points:
        if stretches and point - stretches[-1][1] <= gap:
            stretches[-1][1] = point
        else:
            stretches.append([point, point])
    return stretches


def within(stretches, low, high):
    """The stretches cut to [low, high]."""
    return [[max(a, low), min(b, high)] for a, b in stretches if b >= low and a <= high]


def merged(stretches):
    result = []
    for a, b in sorted(stretches):
        if result and a - result[-1][1] <= PROFILE_GAP:
            result[-1][1] = max(result[-1][1], b)
        else:
            result.append([a, b])
    return result


class Polling:
    """When the collectors whose samples a donation figure reads were polling each account.

    Before an account's first clan roster sample its figures read profile counters: the record's
    profile poll spans, and the old health file's hourly captures while it listed the account in a
    clan. From that sample on they read roster samples: the clan's roster polls while the record
    lists the account in that clan. Unchanged values are not stored, so a poll without a sample
    still observed the account.
    """

    def __init__(self, connection):
        self.connection = connection
        self.rosters = {}
        self.captures = runs([row[0] for row in connection.execute(
            "SELECT DISTINCT c.sampled_at FROM player_counters c JOIN collection_runs r ON r.id=c.source_run_id "
            "WHERE r.collector='profile_import' ORDER BY 1")], PROFILE_GAP)

    def roster(self, clan):
        if clan not in self.rosters:
            self.rosters[clan] = runs([row[0] for row in self.connection.execute(
                "SELECT COALESCE(fetched_at,started_at) AS at FROM collection_items WHERE source_kind='clan_roster' "
                "AND scope_id=? AND status IN ('ok','empty') AND json_extract(completeness_json,'$.complete')=1 "
                "ORDER BY at", (clan,))], ROSTER_GAP)
        return self.rosters[clan]

    def account(self, account):
        execute = self.connection.execute
        first = execute("SELECT MIN(sampled_at) FROM donation_snapshots WHERE account_id=? "
                        "AND donations IS NOT NULL", (account,)).fetchone()[0]
        cut = UNBOUNDED if first is None else first
        early = [stretch for start, last in execute(
            "SELECT from_at,last_observed_at FROM old_clan_stays WHERE account_id=?", (account,))
            for stretch in within(self.captures, start, last)]
        early.extend([start, last] for start, last, fields in execute(
            "SELECT from_at,last_observed_at,completeness_json FROM player_coverage WHERE account_id=?", (account,))
            if json.loads(fields).get("fields"))
        polled = within(early, 0, cut)
        if first is not None:
            for clan, start, end in execute(
                    "SELECT c.tag,m.from_at,m.to_at FROM clan_membership m JOIN clans c ON c.id=m.clan_id "
                    "WHERE m.account_id=? AND c.is_family=1", (account,)):
                polled.extend(within(self.roster(clan), max(start, cut), UNBOUNDED if end is None else end))
        return merged(polled)

    def account_clans(self, account, selected):
        """Roster polling only in the selected source clans; profile counters have no source clan."""
        polled = []
        for clan, tag, start, end in self.connection.execute(
                'SELECT c.id,c.tag,m.from_at,m.to_at FROM clan_membership m JOIN clans c ON c.id=m.clan_id '
                'WHERE m.account_id=?', (account,)):
            if clan in selected:
                polled.extend(within(self.roster(tag), start, UNBOUNDED if end is None else end))
        return merged(polled)


def coverage(polled, samples, falls, after, before):
    """C, P or N for one account's window.

    C: polled at its start and end, and polled on schedule leading into every counter fall, so only
    donations after the last poll before a fall can be missing. Unchanged values are not stored, so a
    rise across a gap without a fall is still counted in full. P: sampled or polled, but not that.
    N: neither.
    """
    def at(moment):
        return any(a <= moment <= b + PROFILE_GAP for a, b in polled)

    if at(after) and at(before) and all(any(a < fall <= b for a, b in polled) for fall in falls):
        return "C"
    return "P" if samples or any(a < before and b > after for a, b in polled) else "N"


def counted_clans(points, selected, after, before):
    """Attribute rises to their sample's clan, starting a new counter on every observed clan change."""
    totals = dict.fromkeys((*DONATION_FIELDS, 'samples'), 0)
    previous, falls = None, []
    for at, clan, given, received in points:
        if at >= before:
            break
        if at > after and clan in selected:
            totals['samples'] += 1
            changed = previous is not None and clan != previous[0]
            fell = False
            for index, name in enumerate(DONATION_FIELDS, 1):
                value = (given, received)[index - 1]
                last = None if previous is None else previous[index]
                fell = fell or last is not None and value < last
                totals[name] += value if last is None or changed or value < last else value - last
            if changed or fell:
                falls.append(at)
        previous = (clan, given, received)
    return totals, falls


def donation_windows(connection, selected, windows, clans=None):
    """Each account's donations and received in each (after, before) window, with how far polling covers it."""
    polling, result = Polling(connection), []
    clan_ids = None if clans is None else {row[0] for row in connection.execute(
        f'SELECT id FROM clans WHERE family_code IN ({placeholders(clans)})', clans)}
    for tag in selected:
        account = connection.execute("SELECT id FROM accounts WHERE player_tag=?", (tag,)).fetchone()
        points = donation_points(connection, account[0], UNBOUNDED) if account else []
        polled = polling.account(account[0]) if account else []
        if clan_ids is not None:
            clan_points = connection.execute(
                'SELECT sampled_at,clan_id,donations,received FROM donation_snapshots WHERE account_id=? '
                'AND clan_id IS NOT NULL AND donations IS NOT NULL AND received IS NOT NULL ORDER BY sampled_at',
                (account[0],)).fetchall() if account else []
            clan_polled = polling.account_clans(account[0], clan_ids) if account else []
        for after, before in windows:
            totals, falls = counted(points, after, before)
            state = coverage(polled, totals["samples"], falls, after, before)
            result.append({"player_tag": tag, "after": after, "before": before, "coverage": state,
                           **{name: None if state == "N" else totals[name] for name in DONATION_FIELDS}})
            if clan_ids is not None:
                clan_totals, clan_falls = counted_clans(clan_points, clan_ids, after, before)
                clan_state = coverage(clan_polled, clan_totals['samples'], clan_falls, after, before)
                result[-1].update(clan_coverage=clan_state, **{
                    'clan_' + name: None if clan_state == 'N' else clan_totals[name] for name in DONATION_FIELDS})
    return {"windows": [list(window) for window in windows], "rows": result}


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

    async def latest_donation_samples(self, player_tags, *, before):
        return await self._read(latest_donation_samples, checked_tags(player_tags), timestamp(before))

    async def donation_windows(self, player_tags, *, windows, clan_codes=None):
        """Donations and received per account and window, with how far the collectors polled it."""
        checked = []
        for after, before in windows:
            after, before = timestamp(after), timestamp(before)
            if before <= after:
                raise ValueError("Expected a window that ends after it starts")
            checked.append((after, before))
        if not checked:
            raise ValueError("Expected at least one window")
        clans = None if clan_codes is None else tuple(clan_codes)
        return await self._read(donation_windows, checked_tags(player_tags), tuple(checked), clans)

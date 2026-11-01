"""Regular wars and current wars from the family record.

The agent's regular-war participation and current wars come from the shared
war reads. League wars keep their stored league history adapter. Rows keep
the attendance shape, so attendance, cohorts, trials and hibernation join
them unchanged.

Regular means wars the record holds as regular, including early wars whose
times cannot rule out a friendly war; those are counted in coverage. Wars the
record proves friendly are counted in coverage, never mixed in.
"""

from collections import Counter
from datetime import datetime, timezone
import sqlite3

from elbow_helper.configuration.clans import CLANS
from elbow_helper.data.reads import core as read_core
from elbow_helper.features.wars.record import board_payload, clash_time, record_fresh
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigrationError

SECOND = 1_000_000
DAY = 86400 * SECOND
# One clan's wars over a year stay far below the read's war bound.
CHUNK = 365 * DAY
# Lineup rows are read for as many wars as fit one page of the read.
PAGE_ROWS = 200
# A Clash pause moves a war's end, and with it an old war ID, by up to this much.
CAPTURE_WINDOW = 72 * 3600 * SECOND
# Looking back for the latest wars stops here; the family record holds nothing older.
FLOOR = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()) * SECOND
CODES = {clan.tag: code for code, clan in CLANS.items()}
FAILURES = (OSError, sqlite3.Error, TimeoutError, SQLiteMigrationError)
UNAVAILABLE = {'error': 'Stored war evidence is unavailable.', 'cause': 'source_unavailable'}
BASIS = ('family record regular wars ended in the selection; each war keeps its old IDs as aliases. '
         'Friendly wars are counted apart, never mixed in.')


def war_reads(context):
    return getattr(getattr(context, 'bot', None), 'war_reads', None)


def seconds(value):
    return None if value is None else value // SECOND


def iso(value):
    return None if value is None else datetime.fromtimestamp(value / SECOND, timezone.utc).isoformat()


def war_id(view):
    """The old clan-health ID of a regular war, so follow-up selections keep working."""
    return f"REG:{view['clan_tag']}:{clash_time(view['end_at'])}:{view['opponent']['clan_tag']}"


def aliases(view):
    """The war's own ID and every old ID the clan's old data used for it."""
    code = CODES[view['clan_tag']]
    return sorted({war_id(view), *(row['legacy_id'] for row in view['legacy_ids'] if row['clan_code'] == code)})


def id_window(value):
    """The clan and end window an old regular war ID names, or None for any other ID."""
    parts = value.split(':') if isinstance(value, str) else []
    if len(parts) != 4 or parts[0] != 'REG' or parts[1] not in CODES:
        return None
    try:
        end = datetime.strptime(parts[2], '%Y%m%dT%H%M%S.%fZ').replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    end = round(end.timestamp() * SECOND)
    return parts[1], max(0, end - CAPTURE_WINDOW), end + CAPTURE_WINDOW + 1


async def pages(method, *args, **kwargs):
    rows, coverage, offset = [], [], 0
    while offset is not None:
        page = await method(*args, **kwargs, limit=PAGE_ROWS, offset=offset)
        rows.extend(page['rows'])
        coverage.append(page['coverage'])
        offset = page['next_offset']
    return rows, coverage


class Selection:
    """Wars and lineup rows gathered over several bounded reads at one as_of."""

    def __init__(self, reads, at):
        self.reads, self.at = reads, at
        self.wars, self.rows, self.coverage, self.friendly = {}, {}, [], 0

    async def wars_in(self, tag, after, before, *, enough=None, friendly=False):
        """One clan's regular wars that ended in [after, before), newest first.

        With ``enough``, stops once that many are found: every war not read is older.
        """
        found, end = [], before
        while end > after and (enough is None or len(found) < enough):
            start, offset = max(after, end - CHUNK), 0
            while offset is not None and (enough is None or len(found) < enough):
                page = await self.reads.completed([tag], start, end, as_of=self.at, limit=PAGE_ROWS, offset=offset)
                found.extend(page['rows'])
                self.coverage.append(page['coverage'])
                offset = page['next_offset']
            if friendly:
                other = await self.reads.completed([tag], start, end, types=('FRIENDLY',), as_of=self.at, limit=1)
                self.friendly += other['coverage']['wars_selected']
            end = start
        return found

    async def add(self, tag, views):
        """Keep these wars and read their lineup rows, as many wars per read as fill one page."""
        batches, size = [], 0
        for view in sorted(views, key=lambda view: view['end_at']):
            self.wars[view['war_id'], tag] = view
            members = view['coverage']['own']['members']
            if not batches or size + members > PAGE_ROWS:
                batches.append([])
                size = 0
            batches[-1].append(view)
            size += members
        for batch in batches:
            rows, _ = await pages(self.reads.participation, [tag], batch[0]['end_at'], batch[-1]['end_at'] + 1,
                                  as_of=self.at)
            lineups = {(view['war_id'], tag): [] for view in batch}
            for row in rows:
                lineups.setdefault((row['war_id'], tag), []).append(row)
            # A war read again from an overlapping window replaces, never doubles, its rows.
            self.rows.update(lineups)


def account_row(row, view, entry, causes):
    expected, used = row['attacks_expected'], row['attacks_used']
    complete = (row['missed'] is not None and type(expected) is int and expected > 0
                and type(used) is int and 0 <= used <= expected)
    measured = row['attacks_measured']
    return {'war_id': entry['war_id'], 'war_aliases': entry['aliases'], 'clan_code': entry['clan_code'],
            'war_type': 'REG', 'season': '', 'start_ts': seconds(view['start_at']), 'end_ts': seconds(view['end_at']),
            'player_tag': row['player_tag'], 'player_name': row['player_name'],
            'attacks_expected': expected if complete else None, 'attacks_used': used,
            'missed': expected - used if complete else None, 'final_capture': complete,
            'uncertainty_reasons': [] if complete else causes,
            # Every stored attack has its stars and destruction.
            'observed_attack_performance': {
                'attacks': measured, 'stars': row['stars'], 'stars_measured': measured, 'triples': row['triples'],
                'destruction_total': row['destruction'], 'destruction_measured': measured}}


def snapshot(selection, *, requested=None, latest=None, window=False):
    """The old attendance snapshot shape from the record's wars and lineup rows."""
    entries = []
    for key, view in selection.wars.items():
        known = aliases(view)
        if requested is not None and not requested.intersection(known):
            continue
        entries.append({'key': key, 'view': view, 'war_id': war_id(view), 'clan_code': CODES[key[1]],
                        'aliases': known, 'end_at': view['end_at']})
    entries.sort(key=lambda entry: (-entry['end_at'], entry['clan_code'], entry['war_id']))
    if latest is not None:
        entries = entries[:latest]
    selected = {entry['key'][0] for entry in entries}
    conflicted = {war for coverage in selection.coverage for signal in coverage['signals']
                  for group in signal.get('identity_conflicts', ()) for war in group['war_ids']} & selected
    rows, empty, conflicts, sources = [], [], [], []
    for entry in entries:
        view = entry['view']
        own = view['coverage']['own']
        # Early wars copied from old participation rows have no team size; their final list is the lineup.
        if not own['members'] or own['team_size'] is not None and own['members'] < own['team_size']:
            empty.append(entry['war_id'])
        causes = [cause for cause, missing in (
            ('not_final', view['state'] != 'warEnded'), ('lineup_incomplete', not own['lineup_complete']),
            ('attacks_incomplete', not own['attacks_complete'])) if missing]
        if entry['key'][0] in conflicted:
            conflicts.append({'war_id': entry['war_id'], 'reasons': ['shared_clan_pair_and_end']})
        sources.append({'war_id': entry['war_id'], 'clan_code': entry['clan_code'], 'season': '',
                        'aliases': entry['aliases'], 'type_basis': view['type_basis'],
                        'end_ts': seconds(view['end_at']),
                        'sources': sorted({'family_record', *(row['source'] for row in view['legacy_ids'])})})
        rows.extend(account_row(row, view, entry, causes)
                    for row in sorted(selection.rows.get(entry['key'], ()), key=lambda row: row['player_tag']))
    signals = {}
    for coverage in selection.coverage:
        for signal in coverage['signals']:
            signals[signal['signal'], signal['scope']] = signal
    return {'rows': rows, 'war_ids': sorted({entry['war_id'] for entry in entries}),
            'war_aliases': sources, 'wars_without_rosters': sorted(empty),
            'identity_conflicts': conflicts, 'unproven_war_ids': [],
            'duplicate_identities_removed': sum(len(entry['aliases']) - 1 for entry in entries),
            'coverage': {
                'query_complete': True, 'historical_collection_complete': False,
                'exact_ranking': not (conflicts or empty),
                'regular_wars_not_checked_for_friendly': sum(
                    entry['view']['type_basis'] == 'legacy_unchecked' for entry in entries),
                'friendly_wars_not_counted': selection.friendly if window else None,
                'wars_not_final': sum(entry['view']['state'] != 'warEnded' for entry in entries),
                'type_basis': dict(sorted(Counter(entry['view']['type_basis'] for entry in entries).items())),
                'source': 'family_record', 'as_of': iso(selection.at),
                'source_status': [{'clan_code': CODES.get(scope), 'signal': kind, 'status': signal['status'],
                                   'last_good_at': iso(signal['last_good_at'])}
                                  for (kind, scope), signal in sorted(signals.items())],
                'basis': BASIS}}


async def regular_wars(context, *, clan_codes, after_ts=None, before_ts=None, war_ids=(), latest_completed=None):
    """Regular-war participation in the old attendance shape; one selector, as the old read takes."""
    selection = Selection(war_reads(context), read_core.now_us())
    tags = [CLANS[code].tag for code in clan_codes]
    if war_ids:
        requested = set(war_ids)
        windows = [window for window in map(id_window, war_ids) if window is not None and window[0] in tags]
        for tag, after, before in sorted(set(windows)):
            views = await selection.wars_in(tag, after, before)
            await selection.add(tag, [view for view in views if requested.intersection(aliases(view))])
        return snapshot(selection, requested=requested)
    if latest_completed:
        # The newest wars of all clans are among each clan's own newest.
        for tag in tags:
            views = await selection.wars_in(tag, FLOOR, selection.at + 1, enough=latest_completed)
            await selection.add(tag, views[:latest_completed])
        return snapshot(selection, latest=latest_completed)
    for tag in tags:
        await selection.add(tag, await selection.wars_in(tag, after_ts * SECOND, before_ts * SECOND, friendly=True))
    return snapshot(selection, window=True)


def combine(regular, league, latest=None):
    """Regular wars from the record beside league wars from the old adapter, as one selection.

    For the latest wars, both lists are ranked together by war end. A league
    war without a stored lineup has no known end and ranks last.
    """
    if latest:
        ends = {}
        for row in league['rows']:
            ends[row['war_id']] = max(ends.get(row['war_id'], 0), row['end_ts'] or 0)
        wars = [(war['end_ts'], war['clan_code'], war['war_id']) for war in regular['war_aliases']]
        wars += [(ends.get(war['war_id'], -1), war['clan_code'], war['war_id']) for war in league['war_aliases']]
        kept = {war for _, _, war in sorted(wars, key=lambda war: (-war[0], war[1], war[2]))[:latest]}
    else:
        kept = set(regular['war_ids']) | set(league['war_ids'])

    def chosen(source, field):
        return [row for row in source.get(field, []) if row['war_id'] in kept]

    return {'rows': chosen(regular, 'rows') + chosen(league, 'rows'),
            'war_ids': sorted(kept),
            'war_aliases': chosen(regular, 'war_aliases') + chosen(league, 'war_aliases'),
            'wars_without_rosters': sorted(war for source in (regular, league)
                                           for war in source.get('wars_without_rosters', []) if war in kept),
            'identity_conflicts': chosen(regular, 'identity_conflicts') + chosen(league, 'identity_conflicts'),
            'unproven_war_ids': sorted(war for war in league.get('unproven_war_ids', []) if war in kept),
            'duplicate_identities_removed': (regular['duplicate_identities_removed']
                                             + league.get('duplicate_identities_removed', 0)),
            'coverage': {
                'query_complete': True, 'historical_collection_complete': False,
                'exact_ranking': regular['coverage']['exact_ranking'] and league['coverage'].get('exact_ranking', False),
                'regular_wars_not_checked_for_friendly': regular['coverage']['regular_wars_not_checked_for_friendly'],
                'friendly_wars_not_counted': regular['coverage']['friendly_wars_not_counted'],
                'regular': regular['coverage'], 'league': league['coverage'],
                'basis': 'Regular wars from the family record and league wars from stored league history.'}}


async def current_wars(context, codes):
    """Each clan's latest current-war check, in the shape of a live Clash read.

    A failed, overdue or missing check gives no war, as a failed live read did.
    """
    reads, items = war_reads(context), []
    for code in codes:
        tag = CLANS[code].tag
        current = await reads.current(tag, lineups=True)
        signal = current['coverage']['signals'][0]
        if not record_fresh(current) or current['state'] != 'notInWar' and current['war'] is None:
            status = signal['status'] if signal['status'] not in ('observed', 'partial') else 'unavailable'
            items.append({'tag': tag, 'status': status, 'observed_at': iso(signal['last_good_at'])})
            continue
        war = current['war']
        data = board_payload(war) if war is not None else {'state': 'notInWar'}
        # An attack whose attacker is unknown is missing from the list, never from the count.
        used = {member['player_tag']: member['attacks_used'] for member in (war or {}).get('clan', {}).get('members', ())
                if member['attacks_used'] is not None}
        items.append({'tag': tag, 'status': 'ok', 'data': data, 'attacks_used': used,
                      'observed_at': iso(current['checked_at'])})
    return {'items': items}

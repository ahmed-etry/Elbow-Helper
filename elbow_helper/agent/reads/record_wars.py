"""Compatibility projections for regular and league attendance snapshots.

Selection, aliases and compact participation belong to WarReads. This module
only preserves the agent's retained attendance shape and combines CWL rows.
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone
import sqlite3

from elbow_helper.configuration.clans import CLANS
from elbow_helper.data.reads import core as read_core
from elbow_helper.data.reads.war_selection import regular_id as war_id
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigrationError

SECOND = 1_000_000
CODES = {clan.tag: code for code, clan in CLANS.items()}
FAILURES = (OSError, sqlite3.Error, TimeoutError, SQLiteMigrationError)
UNAVAILABLE = {'error': 'Stored war evidence is unavailable.', 'cause': 'source_unavailable'}
BASIS = ('family record regular wars ended in the selection; each war keeps its old IDs as aliases. '
         'Friendly wars are counted apart, never mixed in.')
__all__ = ['war_id', 'war_reads', 'regular_wars', 'snapshot', 'combine', 'FAILURES', 'UNAVAILABLE']


def war_reads(context):
    return getattr(getattr(context, 'bot', None), 'war_reads', None)


def seconds(value):
    return None if value is None else value // SECOND


def iso(value):
    return None if value is None else datetime.fromtimestamp(value / SECOND, timezone.utc).isoformat()


def snapshot(source):
    """The old attendance snapshot shape from the record's wars and lineup rows."""
    entries = []
    identities = {}
    for view in source['wars']:
        identifier = identities.setdefault(view['war_id'], view['selection_id'])
        entries.append({'key': (view['war_id'], view['clan_tag']), 'view': view,
                        'war_id': identifier, 'clan_code': CODES[view['clan_tag']],
                        'aliases': view['aliases'], 'end_at': view['end_at']})
    entries.sort(key=lambda entry: (-entry['end_at'], entry['clan_code'], entry['war_id']))
    selected = {entry['key'][0] for entry in entries}
    conflicted = {war for signal in source['coverage']['signals']
                  for group in signal.get('identity_conflicts', ()) for war in group['war_ids']} & selected
    lineups = defaultdict(list)
    for row in source['rows']:
        lineups[row['war_id'], row['clan_tag']].append(row)
    rows, empty, conflicts, sources = [], [], [], []
    for entry in entries:
        view = entry['view']
        own = view['coverage']['own']
        # Early wars copied from old participation rows have no team size; their final list is the lineup.
        if not own['members'] or own['team_size'] is not None and own['members'] < own['team_size']:
            empty.append(entry['war_id'])
        if entry['key'][0] in conflicted:
            conflicts.append({'war_id': entry['war_id'], 'reasons': ['shared_clan_pair_and_end']})
        sources.append({'war_id': entry['war_id'], 'clan_code': entry['clan_code'], 'season': '',
                        'aliases': entry['aliases'], 'type_basis': view['type_basis'],
                        'end_ts': seconds(view['end_at']),
                        'sources': sorted({'family_record', *(row['source'] for row in view['legacy_ids'])})})
        rows.extend({**{key: value for key, value in row.items()
                       if key not in ('war_id', 'clan_tag', 'type', 'start_at', 'end_at')},
                     'war_id': entry['war_id'], 'war_aliases': entry['aliases'],
                     'clan_code': entry['clan_code'], 'war_type': 'REG', 'season': '',
                     'start_ts': seconds(row['start_at']), 'end_ts': seconds(row['end_at'])}
                    for row in lineups[entry['key']])
    signals = {(signal['signal'], signal['scope']): signal for signal in source['coverage']['signals']}
    return {'rows': rows, 'war_ids': sorted({entry['war_id'] for entry in entries}),
            'war_aliases': sources, 'wars_without_rosters': sorted(empty),
            'identity_conflicts': conflicts, 'unproven_war_ids': [],
            'duplicate_identities_removed': sum(len(entry['aliases']) - 1 for entry in entries),
            'coverage': {
                'query_complete': True, 'historical_collection_complete': False,
                'exact_ranking': not (conflicts or empty),
                'regular_wars_not_checked_for_friendly': len({
                    entry['key'][0] for entry in entries if entry['view']['type_basis'] == 'legacy_unchecked'}),
                'friendly_wars_not_counted': source['friendly_wars_not_counted'],
                'wars_not_final': len({entry['key'][0] for entry in entries if entry['view']['state'] != 'warEnded'}),
                'type_basis': dict(sorted(Counter({entry['key'][0]: entry['view']['type_basis']
                                                  for entry in entries}.values()).items())),
                'source': 'family_record', 'as_of': iso(source['as_of']),
                'source_status': [{'clan_code': CODES.get(scope), 'signal': kind, 'status': signal['status'],
                                   'last_good_at': iso(signal['last_good_at'])}
                                  for (kind, scope), signal in sorted(signals.items())],
                'basis': BASIS}}


async def regular_wars(context, *, clan_codes, after_ts=None, before_ts=None, war_ids=(), latest_completed=None):
    """Project the shared selection without implementing a second selector."""
    source = await war_reads(context).regular_selection(
        [CLANS[code].tag for code in clan_codes],
        after=None if after_ts is None else after_ts * SECOND,
        before=None if before_ts is None else before_ts * SECOND,
        war_ids=war_ids, latest_completed=latest_completed, as_of=read_core.now_us())
    return snapshot(source)


def combine(regular, league, latest=None):
    """Regular wars beside league wars, both from the record, as one selection.

    For the latest wars, both lists are ranked together by war end. A league
    war without a stored lineup has no known end and ranks last.
    """
    if latest:
        ends = {}
        for row in league['rows']:
            ends[row['war_id']] = max(ends.get(row['war_id'], 0), row['end_ts'] or 0)
        wars = [(war['end_ts'], war['clan_code'], war['war_id']) for war in regular['war_aliases']]
        wars += [(ends.get(war['war_id'], -1), war['clan_code'], war['war_id']) for war in league['war_aliases']]
        ordered = sorted(wars, key=lambda war: (-war[0], war[1], war[2]))
        kept = set(list(dict.fromkeys(war for _, _, war in ordered))[:latest])
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
                'basis': 'Regular and league wars from the family record.'}}

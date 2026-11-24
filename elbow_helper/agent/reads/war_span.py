"""How far back the stored wars go, beside the wars a war read checked."""

import asyncio
from datetime import datetime, timezone

from elbow_helper.configuration.clans import CLANS
from elbow_helper.data.reads import core as read_core
from elbow_helper.domain.player_tags import normalize_player_tag
from . import record_wars
from .cache import source_revision

TYPES = {'REG': ('REG',), 'CWL': ('CWL',), 'both': ('REG', 'CWL')}
MEANINGS = {
    'record_span': 'UTC war end dates from the family record.',
    'checked': 'First and last war end of the wars this read selected, and how many.',
    'recorded': 'First and last war end of every recorded war of this type in the selected clans, and how many.',
    'accounts': "The account's recorded wars of this type in any family clan, and how many ended outside the "
                'checked period: the chosen dates or season (period chosen_dates), '
                'or else the first to last checked war end (period checked_wars).'}


def month(key):
    """A season's month as [after, before) epoch seconds."""
    start = datetime.fromisoformat(key[:7] + '-01').replace(tzinfo=timezone.utc)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return int(start.timestamp()), int(end.timestamp())


def chosen_dates(context, arguments):
    """The dates or seasons a selection chose, as [after, before) epoch seconds; None when it chose wars."""
    from .wars import single_selection
    _, after, before = single_selection(context, arguments)
    if after is not None:
        return [(after, before)]
    seasons = arguments.get('season_keys') or ([arguments['season']] if arguments.get('season') else [])
    return [month(key) for key in sorted({key[:7] for key in seasons})] or None


def checked_ends(source):
    """The selected wars' ends in epoch seconds, from their lineups and war entries."""
    ends = {row['end_ts'] for row in source.get('rows', ()) if row.get('end_ts') is not None}
    ends.update(war['end_ts'] for war in source.get('war_aliases', ()) if war.get('end_ts') is not None)
    return ends


def day(seconds):
    return None if seconds is None else datetime.fromtimestamp(seconds, timezone.utc).date().isoformat()


def span(row):
    """First and last war end and the wars of one record row; none stored is zero wars."""
    row = row or {}
    return {'first_war_end': day(record_wars.seconds(row.get('first_end_at'))),
            'last_war_end': day(record_wars.seconds(row.get('last_end_at'))), 'wars': row.get('wars', 0)}


async def record_span(context, arguments, source, tags=None, *, account_entries=None):
    """The record's first and last wars beside the wars ``source`` checked.

    ``tags`` are the accounts the selection named, or None for clans only.
    A failed read leaves the rest of the result as it is.
    """
    reads = record_wars.war_reads(context)
    if reads is None:
        return {'status': 'unavailable', 'cause': 'missing_setup'}
    kinds = TYPES[arguments['war_type']]
    ends = checked_ends(source)
    windows = chosen_dates(context, arguments)
    period = 'chosen_dates' if windows is not None else 'checked_wars'
    if windows is None:
        windows = [(min(ends), max(ends) + 1)] if ends else []
    named = {normalize_player_tag(tag): tag for tag in tags or ()}
    named.pop(None, None)
    listed = sorted(named)
    dependencies = getattr(context, 'read_dependencies', None)
    if dependencies is not None:
        # Taken before the read, as the other war reads do.
        revision = (await asyncio.to_thread(source_revision, reads)
                    if getattr(context, 'read_cache', None) is not None else None)
        dependencies.observe(context, reads, revision)
    try:
        # The record accepts 200 account tags per read.
        found = {'recorded': [], 'accounts': []}
        as_of = read_core.now_us()
        for start in range(0, max(1, len(listed)), 200):
            batch = await reads.record_span(
                [CLANS[code].tag for code in arguments.get('clan_codes', list(CLANS))], types=kinds,
                player_tags=listed[start:start + 200], family=[clan.tag for clan in CLANS.values()],
                windows=[(after * record_wars.SECOND, before * record_wars.SECOND) for after, before in windows],
                as_of=as_of)
            found['recorded'] = batch['recorded']
            found['accounts'].extend(batch['accounts'])
    except (*record_wars.FAILURES, ValueError):
        return {'status': 'unavailable', 'cause': 'source_unavailable'}
    recorded = {row['type']: row for row in found['recorded']}
    accounts = {(row['player_tag'], row['type']): row for row in found['accounts']}
    result = {'status': 'read',
              'checked': {'first_war_end': day(min(ends)) if ends else None,
                          'last_war_end': day(max(ends)) if ends else None,
                          'wars': len(source.get('war_ids', ())), 'period': period},
              'recorded': [{'war_type': kind, **span(recorded.get(kind))} for kind in kinds]}
    if tags is not None:
        result['accounts'] = [{'player_tag': named[tag], 'war_type': kind, **span(accounts.get((tag, kind))),
                               'wars_outside_checked': (accounts.get((tag, kind)) or {}).get('outside', 0)}
                              for tag in listed for kind in kinds]
    result['meanings'] = MEANINGS
    if account_entries is not None:
        for entry in result.pop('accounts', []):
            tag = entry.pop('player_tag')
            spans = account_entries[tag].setdefault('record_span', [])
            if entry['wars']:
                spans.append(entry)
        result['meanings'] = {('rows.family_wars.record_span' if key == 'accounts' else key): value
                              for key, value in MEANINGS.items()}
        result['meanings']['rows.family_wars.record_span'] += (
            ' Within a checked account\'s record_span list, omitted types have no recorded wars.')
    return result

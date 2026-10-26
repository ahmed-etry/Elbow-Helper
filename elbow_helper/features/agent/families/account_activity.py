"""Dated account evidence composed from the existing war and ranked readers."""

import asyncio
import sqlite3

from .model_view import pick, ranked_view
from .war_reads import attendance, period


HISTORY_KINDS = ('regular_war', 'cwl', 'ranked')
ACTIVITY_FIELDS = ('player_tag', 'war_ids', 'attacks_used', 'attacks_expected', 'missed',
                   'miss_rate', 'unconfirmed_opportunities', 'totals_complete',
                   'observation_status', 'participation_status', 'attack_performance')


async def account_activity(context, rows, arguments):
    from .ranked_reads import ranked_activity

    window = {key: arguments[key] for key in ('after', 'before') if arguments.get(key)}
    window.setdefault('before', context.source_message.created_at.isoformat())
    period(window)
    tags = sorted({row['player_tag'] for row in rows if row.get('player_tag')})
    if not tags:
        return {}
    selection = {**window, 'account_tags': tags}

    async def read(reader, selected):
        try:
            return await reader(context, selected)
        except (OSError, sqlite3.Error, TimeoutError):
            return {'cause': 'source_unavailable', 'status': 'failed'}

    sources = await asyncio.gather(
        read(attendance, {**selection, 'war_type': 'REG', 'unit': 'account'}),
        read(attendance, {**selection, 'war_type': 'CWL', 'unit': 'account'}),
        read(ranked_activity, selection),
    )
    history = {}
    for kind, source in zip(HISTORY_KINDS, sources):
        failed = bool(source.get('error') or source.get('status') == 'failed')
        invalid = source.get('cause') in ('invalid_arguments', 'needs_input', 'empty_cohort')
        status = 'invalid_selection' if invalid else 'failed' if failed else 'complete'
        records = {row['player_tag']: row for row in source.get('rows', []) if row.get('player_tag')}
        history[kind] = {'read_ran': not invalid, 'status': status,
                         **window, **pick(source, ('error', 'cause', 'coverage', 'source_coverage',
                            'history_coverage', 'collection_limits', 'source_complete', 'live_coverage'))}
        for row in rows:
            tag = row.get('player_tag')
            if tag is None:
                continue
            observed = records.get(tag)
            if failed:
                value = {'status': status, **pick(source, ('error', 'cause'))}
            elif observed is None:
                value = {'status': 'empty'}
            elif kind == 'ranked':
                value = ranked_view(observed)
                # The profile status is separate from the activity observation.
                value['status'] = ('observed' if observed.get('attacks_used') is not None
                    or observed.get('observed_attacks') is not None else 'unknown')
            else:
                value = {'status': 'observed' if observed.get('war_ids') else 'empty',
                         **pick(observed, ACTIVITY_FIELDS)}
            row.setdefault('activity', {})[kind] = value
    return history

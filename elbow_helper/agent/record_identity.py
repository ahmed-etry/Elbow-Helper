"""Dated identity facts beside answers from the existing current-state sources."""

import sqlite3

from elbow_helper.data.store.legacy_links import LinkConflict
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigrationError
from .access import require_evidence_access


def fresh(signal):
    return signal.get('source_complete', False) and signal.get('status') == 'observed'


async def record(context, source, method, *args, **kwargs):
    reader = getattr(getattr(context, 'bot', None), source, None)
    if reader is None:
        return None
    await require_evidence_access(context)
    try:
        result = await getattr(reader, method)(*args, **kwargs)
    except (OSError, sqlite3.Error, TimeoutError, LinkConflict, SQLiteMigrationError):
        return None
    await require_evidence_access(context)
    return result


async def account_histories(context, tags):
    """Dated ownership, clan stays and names; never current profile fields."""
    histories = {}
    for start in range(0, len(tags), 200):
        batch = await record(context, 'people_reads', 'account_history', tags[start:start + 200])
        if batch is not None:
            histories.update((row['player_tag'], row['record_history']) for row in batch['rows']
                             if row['record_history'] is not None)
    return histories


async def aliases(context, queries):
    """Feed recorded names into the existing live name resolver."""
    accounts = {}
    for query in queries:
        offset = 0
        while True:
            batch = await record(context, 'people_reads', 'resolve', query, offset=offset)
            if batch is None:
                break
            for row in batch['rows']:
                if row['kind'] == 'account':
                    accounts[row['player_tag']] = row['names']
            offset = batch['next_offset']
            if offset is None:
                break
    return accounts


async def history(context, after, before):
    """Rebuild the existing bounded observation view from dated stays."""
    start, end = int(after.timestamp() * 1_000_000), int(before.timestamp() * 1_000_000)
    generations = await record(context, 'membership_reads', 'generations', start, end)
    if generations is None or not generations['rows']:
        return None
    observed_start = generations['rows'][0]['at']
    spans, offset = [], 0
    while True:
        batch = await record(context, 'membership_reads', 'history', observed_start, end, offset=offset)
        if batch is None or any(not fresh(s) or s['collection_start'] > start
                                or s['gaps'] or s['failures'] for s in batch['coverage']['signals']):
            return None
        spans.extend(batch['rows'])
        offset = batch['next_offset']
        if offset is None:
            break
    runs, rows = [], []
    for generation in generations['rows']:
        at, run = generation['at'], generation['run_id']
        runs.append({'run_id': run, 'created_ts': at / 1_000_000})
        for stay in spans:
            if stay['from_at'] <= at and (stay['to_at'] is None or stay['to_at'] > at):
                rows.append({'run_id': run, 'clan_code': stay['clan_code'],
                             'player_tag': stay['player_tag'], 'player_name': stay['player_name']})
                if len(rows) > 100_000:
                    return None
    return runs, rows, generations['truncated']

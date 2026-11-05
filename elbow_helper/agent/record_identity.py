"""Dated identity facts beside answers from the existing current-state sources."""

from datetime import datetime, timezone
import sqlite3

from elbow_helper.data.store.legacy_links import LinkConflict
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigrationError
from .access import require_evidence_access


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


async def stored_profiles(context, tags):
    """Each account's latest stored profile summary; None when the record cannot be read."""
    profiles = {}
    for start in range(0, len(tags), 200):
        batch = await record(context, 'people_reads', 'account_summary', tags[start:start + 200])
        if batch is None:
            return None
        profiles.update((row['player_tag'], row) for row in batch['rows'])
    return profiles


def observed(value):
    return None if value is None else datetime.fromtimestamp(value / 1_000_000, timezone.utc).isoformat()


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

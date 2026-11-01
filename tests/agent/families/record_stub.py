"""Test helpers for agent reads whose regular wars come from the family record.

Real record reads are covered in ``test_record_wars``. Tests about joins
around them (hibernation, trials, cohorts) serve regular wars from a
synthetic snapshot in the same attendance shape instead.
"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.families import record_wars


def with_record(context, reads=None):
    """The context with the bot's family record war reads."""
    return replace(context, bot=SimpleNamespace(war_reads=reads if reads is not None else SimpleNamespace()))


def record_shaped(snapshot):
    """A stored-war snapshot with the fields the record's regular wars add: each war's end, friendly count."""
    ends = {}
    for row in snapshot['rows']:
        ends[row['war_id']] = max(ends.get(row['war_id'], 0), row['end_ts'] or 0)
    return {**snapshot, 'war_aliases': [{**war, 'end_ts': ends.get(war['war_id'], 0)} for war in snapshot['war_aliases']],
            'coverage': {'friendly_wars_not_counted': None, **snapshot['coverage']}}


def regular_wars_from(repository):
    """Serve the record's regular wars from a synthetic stored-war repository."""
    return patch.object(record_wars, 'regular_wars', AsyncMock(
        side_effect=lambda context, **selection: record_shaped(repository.attendance(war_type='REG', **selection))))

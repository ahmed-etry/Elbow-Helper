"""Worker-thread, single-snapshot read batches and shared argument bounds."""

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction
from ..store import now_us

MAX_PAGE = 200
MAX_SELECTORS = 200


def timestamp(value=None):
    value = now_us() if value is None else value
    if type(value) is not int or value < 0:
        raise ValueError("Expected UTC epoch microseconds")
    return value


def identifiers(values):
    values = tuple(values)
    if len(values) > MAX_SELECTORS or any(type(v) is not int or v <= 0 for v in values):
        raise ValueError("Expected at most 200 positive IDs")
    return tuple(sorted(set(values)))


def tags(values):
    values = tuple(values)
    if len(values) > MAX_SELECTORS:
        raise ValueError("Expected at most 200 tags")
    result = [normalize_player_tag(value) for value in values]
    if any(value is None for value in result):
        raise ValueError("Invalid Clash tag")
    return tuple(sorted(set(result)))


def pagination(limit, offset):
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE or type(offset) is not int or offset < 0:
        raise ValueError("Expected a page of 1..200 rows and a nonnegative offset")


def page(rows, limit, offset):
    more = len(rows) > limit
    return {"rows": rows[:limit], "next_offset": offset + limit if more else None,
            "truncated": more}


def placeholders(values):
    return ",".join("?" for _ in values)


class ReadSnapshot:
    """Memoize shared evidence only for the lifetime of one read transaction."""
    def __init__(self, connection):
        self.execute = connection.execute
        self.coverage_cache = {}
        self.leaf_cache = {}


class RecordReads:
    def __init__(self, store):
        self.store = store

    async def _read(self, operation, *args):
        return await self.store.call(self._snapshot, operation, args)

    @staticmethod
    def _snapshot(connection, operation, args):
        # FamilyStore supplies the worker and connection lifecycle. No fact,
        # checkpoint, or legacy file can be mutated by a read callback.
        connection.execute("PRAGMA query_only=ON")
        with sqlite_transaction(connection):
            return operation(ReadSnapshot(connection), *args)

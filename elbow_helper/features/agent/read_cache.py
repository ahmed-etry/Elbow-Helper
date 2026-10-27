"""Bounded raw reads; authorization and model projections stay with each reader."""

import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
from time import monotonic


LIVE_READ_SECONDS = 15 * 60
MAX_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 512


def read_scope(context):
    destination = getattr(context, 'delivery_channel', None) or getattr(
        getattr(context, 'source_message', None), 'channel', None)
    return (context.guild.id, context.member.id, getattr(destination, 'id', None),
            bool(getattr(context, 'dm_delivery', False)), bool(getattr(context, 'private_pass', False)))


def _encoded(value):
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, 'payload'):
        return value.payload
    raise TypeError('Unsupported raw read value')


class ReadCache:
    """An application-owned LRU. Keys include requester and delivery destination."""

    def __init__(self):
        self.entries = OrderedDict()
        self.pending = {}
        self.bytes = 0
        self.sequence = 0
        self.versions = {}
        self.active = {}

    def _prune_version(self, key):
        if key not in self.entries and key not in self.active:
            self.versions.pop(key, None)

    def _drop(self, key):
        entry = self.entries.pop(key, None)
        if entry is not None:
            self.bytes -= entry[3]
        self._prune_version(key)

    def begin(self, key):
        self.sequence += 1
        self.versions[key] = self.sequence
        self.active[key] = self.active.get(key, 0) + 1
        return self.sequence

    def finish(self, key):
        self.active[key] -= 1
        if not self.active[key]:
            self.active.pop(key)
        self._prune_version(key)

    def is_current(self, key, version):
        return self.versions.get(key) == version

    def invalidate_history(self, channel_id):
        # Channel-wide invalidation covers every asker, destination and page,
        # including pages whose transport read is still in flight.
        for key in set(self.entries) | set(self.active):
            if isinstance(key, tuple) and key[0] == 'history' and key[3] == channel_id:
                self.sequence += 1
                self.versions[key] = self.sequence
                self._drop(key)

    def peek(self, key, *, max_age=None):
        entry = self.entries.get(key)
        if entry is None:
            return None
        if max_age is not None and monotonic() - entry[2] >= max_age:
            self._drop(key)
            return None
        self.entries.move_to_end(key)
        return deepcopy(entry[0]), entry[1]

    def put(self, key, value, observed_at=None, *, version=None, fetched_at=None):
        if version is None:
            self.sequence += 1
            version = self.sequence
            self.versions[key] = version
        if not self.is_current(key, version):
            return
        size = len(json.dumps(value, ensure_ascii=False, default=_encoded).encode('utf-8'))
        if key in self.entries:
            self.bytes -= self.entries.pop(key)[3]
        if size > MAX_BYTES:
            self._prune_version(key)
            return
        while self.entries and (self.bytes + size > MAX_BYTES or len(self.entries) >= MAX_ENTRIES):
            self._drop(next(iter(self.entries)))
        self.entries[key] = (deepcopy(value), observed_at or datetime.now(timezone.utc).isoformat(),
                             monotonic() if fetched_at is None else fetched_at, size)
        self.bytes += size
        self.versions[key] = version

    async def get(self, key, fetch, *, max_age=None, fresh=False):
        if not fresh:
            entry = self.peek(key, max_age=max_age)
            if entry is not None:
                return *entry, True
            pending = self.pending.get(key)
            if pending is not None:
                value, observed = await asyncio.shield(pending)
                return deepcopy(value), observed, True
        if key not in self.active and len(self.active) >= MAX_ENTRIES:
            # Extra callers read through without expanding retained bookkeeping.
            value = await fetch()
            return deepcopy(value), datetime.now(timezone.utc).isoformat(), False
        version = self.begin(key)
        # The fetch belongs to the cache, not to its first caller. Cancelling
        # either the initiator or a waiter must not cancel another request.
        task = asyncio.create_task(self._fetch(key, fetch, version))
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        if not fresh:
            self.pending[key] = task
        value, observed = await asyncio.shield(task)
        return deepcopy(value), observed, False

    async def _fetch(self, key, fetch, version):
        try:
            value = await fetch()
            observed = datetime.now(timezone.utc).isoformat()
            good = ((not isinstance(value, dict) or 'error' not in value) and getattr(value, 'ok', True)
                    and (not hasattr(value, 'payload') or isinstance(value.payload, dict)))
            if good:
                self.put(key, value, observed, version=version)
            return value, observed
        finally:
            if self.pending.get(key) is asyncio.current_task():
                self.pending.pop(key)
            self.finish(key)


def source_revision(source):
    revision = getattr(source, 'cache_revision', None)
    value = revision() if callable(revision) else None
    return value if isinstance(value, str) and value else None


def normalized_arguments(arguments):
    # These are schema-defined sets, never words or patterns in member text.
    return {name: sorted(set(value)) if name in (
        'clan_codes', 'war_ids', 'season_keys', 'values') and isinstance(value, (list, tuple))
        else value for name, value in arguments.items()}


class ReadDependencies:
    """Revision provenance follows nested reads, irrespective of report family."""

    def __init__(self, initial=()):
        self.versions = dict(initial)
        self.invalid = False

    def inherit(self, versions):
        if not isinstance(versions, dict):
            self.invalid = True
            return
        for name, revision in versions.items():
            if name in self.versions and self.versions[name] != revision:
                self.invalid = True
            self.versions[name] = revision

    def observe(self, context, source, revision):
        for name in ('cwl_queries', 'ranked_queries'):
            if getattr(context, name, None) is source:
                self.inherit({name: revision})

    def checked(self, context):
        if self.invalid or any(revision != source_revision(getattr(context, name, None))
                               for name, revision in self.versions.items() if revision is not None):
            return None
        return dict(self.versions)


async def stored_read(context, source, method, **arguments):
    """Cache only raw stored facts, before joins with live roles, links or profiles.

    A database revision is checked on every use. Unknown sources are read through.
    Any database commit invalidates its old reads, including backfills/corrections.
    """
    async def fetch():
        return await asyncio.to_thread(getattr(source, method), **arguments)
    cache = getattr(context, 'read_cache', None)
    dependencies = getattr(context, 'read_dependencies', None)
    if cache is None:
        if dependencies is not None:
            dependencies.observe(context, source, None)
        return await fetch()
    revision = await asyncio.to_thread(source_revision, source)
    if dependencies is not None:
        dependencies.observe(context, source, revision)
    if revision is None:
        return await fetch()
    key = ('stored', read_scope(context), id(source), revision, method,
           json.dumps(normalized_arguments(arguments), sort_keys=True, separators=(',', ':')))
    result, _, _ = await cache.get(key, fetch, fresh=context.fresh_reads)
    return result

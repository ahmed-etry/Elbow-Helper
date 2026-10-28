"""Bounded raw Clash reads shared only by clones of one request context."""

import asyncio
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import json
from time import monotonic
from ..read_cache import LIVE_READ_SECONDS


MAX_AGE_SECONDS = LIVE_READ_SECONDS
MAX_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 256


class RequestClashReads:
    def __init__(self):
        self.entries = OrderedDict()
        self.pending = {}
        self.bytes = 0
        self.scope = None
        self.generation = 0

    def clear(self):
        self.entries.clear()
        self.pending.clear()
        self.bytes = 0
        self.generation += 1

    def bind(self, scope):
        if self.scope != scope:
            self.clear()
            self.scope = scope

    async def get(self, key, fetch, *, max_age=MAX_AGE_SECONDS):
        now = monotonic()
        for expired, (response, observed, fetched, size) in tuple(self.entries.items()):
            if now - fetched >= MAX_AGE_SECONDS:
                self.bytes -= self.entries.pop(expired)[3]
        entry = self.entries.get(key)
        if entry is not None and now - entry[2] < max_age:
            self.entries.move_to_end(key)
            return deepcopy(entry[0]), entry[1], True
        if key in self.pending:
            response, observed, fetched = await asyncio.shield(self.pending[key])
            if monotonic() - fetched < max_age:
                return deepcopy(response), observed, True
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        generation = self.generation
        try:
            response = await fetch()
            observed = datetime.now(timezone.utc).isoformat()
            fetched = monotonic()
            if response.ok and isinstance(response.payload, dict) and generation == self.generation:
                size = len(json.dumps(response.payload, ensure_ascii=False).encode("utf-8"))
                if key in self.entries:
                    self.bytes -= self.entries.pop(key)[3]
                if size <= MAX_BYTES:
                    while self.entries and (self.bytes + size > MAX_BYTES or len(self.entries) >= MAX_ENTRIES):
                        self.bytes -= self.entries.popitem(last=False)[1][3]
                    self.entries[key] = (deepcopy(response), observed, fetched, size)
                    self.bytes += size
            future.set_result((response, observed, fetched))
            return deepcopy(response), observed, False
        except BaseException:
            future.cancel()
            raise
        finally:
            if self.pending.get(key) is future:
                self.pending.pop(key, None)

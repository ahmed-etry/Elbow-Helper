"""Rolling cost allowance for member requests."""

import asyncio
from datetime import datetime, timedelta
import math
from weakref import WeakValueDictionary

from elbow_helper.agent.conversation.wording import AGENT_DAILY_LIMIT
from elbow_helper.infrastructure.ai.deepseek_pricing import deepseek_cost

DAILY_ALLOWANCE_USD = 0.03
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)


class MemberUsageLimit(RuntimeError):
    """Refuse a request, with an empty reply after the first notice."""

    def __init__(self, reply: str):
        super().__init__(reply)
        self.reply = reply


class MemberUsageLimiter:
    def __init__(self):
        self._locks = WeakValueDictionary()
        self._notices = {}

    def lock(self, guild_id, member_id):
        return self._locks.setdefault((guild_id, member_id), asyncio.Lock())

    async def check(self, archive, guild_id, member_id, now):
        rows = await asyncio.to_thread(archive.member_usage, guild_id, member_id, now - DAY, now)
        charges = []
        for timestamp, usage in rows:
            at = datetime.fromisoformat(timestamp)
            cost = deepseek_cost(
                at, cache_hits=usage.get("cache_hit_tokens", 0),
                cache_misses=usage.get("cache_miss_tokens", 0), output=usage.get("completion_tokens", 0),
            )
            charges.append((at, cost))
        key = (guild_id, member_id)
        self._notices = {member: expiry for member, expiry in self._notices.items() if expiry > now}
        remaining = sum(cost for _, cost in charges)
        allowance = round(DAILY_ALLOWANCE_USD * 10**9)
        if remaining < allowance:
            self._notices.pop(key, None)
            return
        if key in self._notices:
            raise MemberUsageLimit("")
        for at, cost in charges:
            remaining -= cost
            if remaining < allowance:
                expiry = at + DAY
                self._notices[key] = expiry
                hours = max(1, math.ceil((expiry - now) / HOUR))
                raise MemberUsageLimit(AGENT_DAILY_LIMIT.format(n=hours))

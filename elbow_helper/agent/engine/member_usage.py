"""In-memory rolling allowances for member requests."""

from collections import deque
from dataclasses import dataclass, field
import math
from time import monotonic

from ..wording import AGENT_DAILY_LIMIT, AGENT_HOURLY_LIMIT

HOUR_SECONDS = 3600
DAY_SECONDS = 24 * HOUR_SECONDS
BANTER_PER_HOUR = 30
QUESTIONS_PER_HOUR = 10
QUESTIONS_PER_DAY = 40


class MemberUsageLimit(RuntimeError):
    """Refuse a request, with an empty reply after the first notice."""

    def __init__(self, reply: str):
        super().__init__(reply)
        self.reply = reply


@dataclass(eq=False)
class UsageEntry:
    timestamp: float


@dataclass
class MemberUsage:
    banter: deque[UsageEntry] = field(default_factory=deque)
    questions: deque[UsageEntry] = field(default_factory=deque)
    notices: dict[str, float] = field(default_factory=dict)

    def prune(self, now):
        for entries, window in ((self.banter, HOUR_SECONDS), (self.questions, DAY_SECONDS)):
            while entries and entries[0].timestamp <= now - window:
                entries.popleft()
        self.notices = {unit: expiry for unit, expiry in self.notices.items() if expiry > now}


class MemberUsageLimiter:
    """Check and count synchronously so concurrent requests cannot oversubscribe."""

    def __init__(self):
        self._members: dict[tuple[int, int], MemberUsage] = {}
        self._next_prune = 0.0

    def acquire(self, guild_id, member_id, *, question, notify=True):
        now = monotonic()
        if now >= self._next_prune:
            for key, usage in list(self._members.items()):
                usage.prune(now)
                if not (usage.banter or usage.questions or usage.notices):
                    del self._members[key]
            self._next_prune = now + HOUR_SECONDS
        usage = self._members.setdefault((guild_id, member_id), MemberUsage())
        usage.prune(now)
        entries = usage.questions if question else usage.banter
        hourly = [entry for entry in entries if entry.timestamp > now - HOUR_SECONDS]
        if question and len(entries) >= QUESTIONS_PER_DAY:
            unit, expiry, divisor, wording = (
                "daily", entries[0].timestamp + DAY_SECONDS, HOUR_SECONDS, AGENT_DAILY_LIMIT,
            )
        elif len(hourly) >= (QUESTIONS_PER_HOUR if question else BANTER_PER_HOUR):
            unit, expiry, divisor, wording = (
                "hourly", hourly[0].timestamp + HOUR_SECONDS, 60, AGENT_HOURLY_LIMIT,
            )
        else:
            entry = UsageEntry(now)
            entries.append(entry)
            return entry
        reply = ""
        if notify and unit not in usage.notices:
            usage.notices[unit] = expiry
            reply = wording.format(n=max(1, math.ceil((expiry - now) / divisor)))
        raise MemberUsageLimit(reply)

    def promote(self, guild_id, member_id, banter_entry):
        """Reclassify admitted banter only when research can proceed."""
        try:
            self.acquire(guild_id, member_id, question=True, notify=False)
        except MemberUsageLimit:
            return False
        banter = self._members[guild_id, member_id].banter
        if banter_entry in banter:
            banter.remove(banter_entry)
        return True

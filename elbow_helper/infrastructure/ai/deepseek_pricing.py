"""DeepSeek token prices in billionths of a US dollar."""

from datetime import datetime, timezone


def deepseek_cost(at: datetime, *, cache_hits: int, cache_misses: int, output: int) -> int:
    utc = at.astimezone(timezone.utc)
    peak = utc.weekday() < 5 and (1 <= utc.hour < 4 or 6 <= utc.hour < 10)
    return (2 if peak else 1) * (3 * cache_hits + 150 * cache_misses + 600 * output)

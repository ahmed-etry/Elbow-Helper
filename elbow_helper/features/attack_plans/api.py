from typing import Any

from elbow_helper.data.collectors import ProfileDemand

from .constants import LOGGER


# A plan reads the account's current army, so it accepts only a profile from the last few minutes.
PROFILE_MAX_AGE_SECONDS = 5 * 60


async def fetch_player(tag: str, profiles: ProfileDemand) -> dict[str, Any] | None:
    """Ask the collectors for a recent player profile, with the request details the plan checks."""
    if not profiles.configured:
        return None
    result = await profiles.player(
        tag,
        max_age=PROFILE_MAX_AGE_SECONDS,
        attempts=3,
        timeout_seconds=10,
        backoff_seconds=0.75,
    )
    data = dict(result.payload) if result.payload is not None else {"_raw_response": str(result.payload)}
    data["_http_status"] = result.status
    data["_latency_ms"] = result.latency_ms
    data["_attempts"] = result.attempts
    if result.failure in {"network_error", "collector_error", "invalid_tag"}:
        LOGGER.info(
            "Player fetch failed for %s after %s attempt(s): %s",
            tag,
            result.attempts,
            result.failure,
        )
        data["_error"] = "network_error"
    return data

from typing import Any

from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.infrastructure.clash import ClashClient

from .constants import LOGGER


async def fetch_player(tag: str, clash_client: ClashClient) -> dict[str, Any] | None:
    """Fetch a player profile from the Clash API."""
    if not clash_client.configured:
        return None
    response = await clash_client.get(
        f"/players/{encode_clash_tag(tag)}",
        attempts=3,
        timeout_seconds=10,
        backoff_seconds=0.75,
    )
    payload = response.payload_object
    data = dict(payload) if payload is not None else {"_raw_response": str(response.payload)}
    data["_http_status"] = response.status
    data["_latency_ms"] = response.latency_ms
    data["_attempts"] = response.attempts
    if response.error is not None:
        LOGGER.info(
            "Player fetch failed for %s after %s attempt(s): %s",
            tag,
            response.attempts,
            response.error,
        )
        data["_error"] = "network_error"
    return data

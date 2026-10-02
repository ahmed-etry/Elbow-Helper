"""Discord target links that contain no quoted action values."""

from collections.abc import Mapping
from typing import Any


def _identifier(value: Any) -> int | None:
    if (isinstance(value, str) and len(value) <= 20
            and value.isascii() and value.isdecimal()):
        value = int(value)
    return value if type(value) is int and 0 < value < 2**64 else None


def target_links(guild_id: int, values: Mapping[str, Any]) -> tuple[str, ...]:
    links = []
    for field in ("channel_id", "source_channel_id", "target_channel_id", "thread_id",
                  "parent_channel_id", "ticket_channel_id", "channel", "thread"):
        identifier = _identifier(values.get(field))
        if identifier is not None:
            links.append(f"https://discord.com/channels/{guild_id}/{identifier}")
    channel_id = _identifier(values.get("channel_id", values.get(
        "target_channel_id", values.get("source_channel_id", values.get("channel")))))
    message_id = _identifier(values.get("message_id"))
    if channel_id is not None and message_id is not None:
        links.append(f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}")
    for field in ("member_id", "member_ids", "user_id", "user_ids", "applicant_id",
                  "member", "user", "applicant"):
        identifiers = values.get(field, ())
        if not isinstance(identifiers, (list, tuple)):
            identifiers = (identifiers,)
        for identifier in identifiers:
            identifier = _identifier(identifier)
            if identifier is not None:
                links.append(f"https://discord.com/users/{identifier}")
    return tuple(dict.fromkeys(links))

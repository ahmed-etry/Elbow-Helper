"""Metadata-only reads of current support-ticket channels."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Iterable

import discord

from elbow_helper.configuration.channels import SUPPORT_TICKET_CATEGORY


MAX_SUPPORT_TICKET_CHANNELS = 250
_MEMBER_REFERENCE = re.compile(r"<@!?(\d+)>")


@dataclass(frozen=True, slots=True)
class SupportTicketRegistration:
    channel_id: int


@dataclass(frozen=True, slots=True)
class SupportTicketMetadata:
    channel_id: int
    channel_name: str
    owner_member_id: int | None
    owner_status: str
    owner_can_send: bool | None
    created_at: str
    created_ts: int
    last_activity_at: str | None
    last_activity_ts: int | None
    last_activity_age_seconds: int | None
    activity_status: str


@dataclass(frozen=True, slots=True)
class SupportTicketSnapshot:
    observed_at: str
    tickets: tuple[SupportTicketMetadata, ...]


class SupportTicketQueries:
    """Interpret ticket metadata without loading message history or content."""

    def ticket_registrations(self, guild: Any) -> tuple[SupportTicketRegistration, ...]:
        channels = tuple(
            channel for channel in getattr(guild, "channels", ())
            if getattr(channel, "category_id", None) == SUPPORT_TICKET_CATEGORY
            and getattr(channel, "type", None) == discord.ChannelType.text
            and type(getattr(channel, "id", None)) is int
            and getattr(channel, "id") > 0
        )
        if len(channels) > MAX_SUPPORT_TICKET_CHANNELS:
            raise ValueError("Support ticket inventory exceeds its read bound")
        return tuple(SupportTicketRegistration(channel.id) for channel in sorted(
            channels, key=lambda item: item.id,
        ))

    def metadata_snapshot(
        self, channels: Iterable[Any], *, observed_at: datetime | None = None,
    ) -> SupportTicketSnapshot:
        observed = observed_at or datetime.now(timezone.utc)
        if not isinstance(observed, datetime):
            raise ValueError("Invalid support ticket observation time")
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        observed = observed.astimezone(timezone.utc)
        selected = tuple(channels)
        if len(selected) > MAX_SUPPORT_TICKET_CHANNELS:
            raise ValueError("Support ticket inventory exceeds its read bound")
        ids = [getattr(channel, "id", None) for channel in selected]
        if (
            any(type(channel_id) is not int or channel_id <= 0 for channel_id in ids)
            or len(ids) != len(set(ids))
        ):
            raise ValueError("Invalid support ticket selection")

        tickets = []
        for channel in selected:
            if (
                getattr(channel, "category_id", None) != SUPPORT_TICKET_CATEGORY
                or getattr(channel, "type", None) != discord.ChannelType.text
            ):
                raise ValueError("Channel is not a support ticket")
            name = getattr(channel, "name", None)
            created = getattr(channel, "created_at", None)
            if (
                not isinstance(name, str) or not name or len(name) > 100
                or not isinstance(created, datetime) or created.tzinfo is None
            ):
                raise ValueError("Invalid support ticket metadata")
            created = created.astimezone(timezone.utc)
            parsed_owner_id = parse_support_owner_id(getattr(channel, "topic", None))
            owner_id = (
                parsed_owner_id
                if parsed_owner_id is not None and parsed_owner_id > 0
                else None
            )
            owner = channel.guild.get_member(owner_id) if owner_id is not None else None
            if owner_id is None:
                owner_status, owner_can_send = "unidentified", None
            elif owner is None:
                owner_status, owner_can_send = "not_in_guild_cache", None
            else:
                permissions = channel.permissions_for(owner)
                owner_status = "resolved"
                owner_can_send = bool(getattr(permissions, "send_messages", False))
            last_message_id = getattr(channel, "last_message_id", None)
            if last_message_id is None:
                activity_status = "no_message_id"
                last_activity = None
                age_seconds = None
            elif type(last_message_id) is not int or last_message_id <= 0:
                raise ValueError("Invalid support ticket activity identity")
            else:
                last_activity = discord.utils.snowflake_time(last_message_id).astimezone(
                    timezone.utc
                )
                activity_status = "channel_last_message"
                age_seconds = max(0, int((observed - last_activity).total_seconds()))
            tickets.append(SupportTicketMetadata(
                channel_id=channel.id, channel_name=name,
                owner_member_id=owner_id, owner_status=owner_status,
                owner_can_send=owner_can_send,
                created_at=created.isoformat(), created_ts=int(created.timestamp()),
                last_activity_at=(last_activity.isoformat() if last_activity else None),
                last_activity_ts=(int(last_activity.timestamp()) if last_activity else None),
                last_activity_age_seconds=age_seconds,
                activity_status=activity_status,
            ))
        tickets.sort(key=lambda row: (
            row.last_activity_ts is not None,
            row.last_activity_ts or 0, row.created_ts, row.channel_id,
        ))
        return SupportTicketSnapshot(
            observed_at=observed.isoformat(), tickets=tuple(tickets),
        )


def parse_support_owner_id(topic: Any) -> int | None:
    if not isinstance(topic, str) or not topic.strip():
        return None
    match = _MEMBER_REFERENCE.search(topic)
    if match:
        return int(match.group(1))
    raw = topic.strip()
    if raw.isdigit():
        return int(raw)
    return None


__all__ = [
    "MAX_SUPPORT_TICKET_CHANNELS", "SupportTicketMetadata",
    "SupportTicketQueries", "SupportTicketRegistration", "SupportTicketSnapshot",
    "parse_support_owner_id",
]

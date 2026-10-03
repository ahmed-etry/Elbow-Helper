"""Resolve identifiers explicitly named in a request."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from elbow_helper.configuration.clans import CLANS, CLAN_ORDER
from elbow_helper.domain.player_tags import find_player_tags


def requested_channels(question: str, channels: Sequence[Any]) -> frozenset[int]:
    ids = {int(value) for value in re.findall(r"<#(\d+)>", question)}
    names = {value.casefold() for value in re.findall(r"(?<![\w<])#([\w-]+)", question)}
    ids.update(channel.id for channel in channels if getattr(channel, "name", "").casefold() in names)
    return frozenset(ids)


def requested_player_tags(question: str) -> frozenset[str]:
    clan_tags = {clan.tag for clan in CLANS.values()}
    return find_player_tags(question) - clan_tags


def requested_member_ids(question: str) -> frozenset[int]:
    return frozenset(
        member_id for value in re.findall(r"<@!?(\d+)>", question)
        if (member_id := int(value)) > 0
    )


def requested_clans(question: str) -> frozenset[str]:
    code_pattern = "|".join(re.escape(code) for code in CLAN_ORDER)
    codes = set(re.findall(
        rf"(?<![\w#])(?:{code_pattern})(?!\w)", question,
    ))
    normalized_tags = find_player_tags(question)
    codes.update(clan.code for clan in CLANS.values() if clan.tag in normalized_tags)
    return frozenset(codes)


def named_sources(question: str, channels: Sequence[Any]) -> dict[str, frozenset[Any]]:
    return {
        "discord_channel": requested_channels(question, channels),
        "clash_account": requested_player_tags(question),
        "discord_member": requested_member_ids(question),
        "clan": requested_clans(question),
    }

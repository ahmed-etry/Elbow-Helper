"""Link and unlink Clash accounts through the account feature."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord

from ...actions.contracts import ActionClass, ChangePreview, PreparedAction
from ...wording import (
    ACTION_ACCOUNT_ADD_LABEL,
    ACTION_ACCOUNT_ADD_LINE,
    ACTION_ACCOUNT_ADD_UNDO_LABEL,
    ACTION_ACCOUNT_BOARD,
    ACTION_ACCOUNT_LOCATION_CLEAR,
    ACTION_ACCOUNT_NAME_LINE,
    ACTION_ACCOUNT_NO_LINK,
    ACTION_ACCOUNT_PRIMARY_LINE,
    ACTION_ACCOUNT_REASSIGN_LINE,
    ACTION_ACCOUNT_REMOVE_LABEL,
    ACTION_ACCOUNT_REMOVE_LINE,
    ACTION_UNDO_CHANGED,
)
from ...actions.outcomes import ActionOutcome
from ...commands.registry import CommandAdapter


def _workflow(context: Any):
    workflow = context.bot.get_cog("AccountLinks")
    if workflow is None:
        raise ValueError("Account links are unavailable")
    return workflow


async def _member(context: Any, member_id: int):
    member = context.guild.get_member(member_id)
    if member is not None:
        return member
    try:
        return await context.guild.fetch_member(member_id)
    except discord.DiscordException:
        return None


def _add_lines(prepared: Mapping[str, Any], member: Any) -> tuple[str, ...]:
    lines = []
    for row in prepared["rows"]:
        tag = row["player_tag"]
        old = prepared["before"].get(tag)
        if old and int(old["discord_user_id"]) != member.id:
            lines.append(ACTION_ACCOUNT_REASSIGN_LINE.format(
                name=row["player_name_last_seen"], tag=tag,
                old=f"<@{int(old['discord_user_id'])}>", member=member.mention,
            ))
        else:
            lines.append(ACTION_ACCOUNT_ADD_LINE.format(
                name=row["player_name_last_seen"], tag=tag, member=member.mention,
            ))
        if row["is_primary"]:
            lines.append(ACTION_ACCOUNT_PRIMARY_LINE.format(tag=tag))
        if old and old.get("player_name_last_seen") != row["player_name_last_seen"]:
            lines.append(ACTION_ACCOUNT_NAME_LINE.format(
                old=old.get("player_name_last_seen") or tag,
                new=row["player_name_last_seen"],
            ))
        if old and any(old.get(key) for key in (
                "last_seen_clan_tag", "last_seen_clan_code", "last_seen_role")):
            lines.append(ACTION_ACCOUNT_LOCATION_CLEAR.format(tag=tag))
    if prepared["invalid_tags"]:
        lines.append('Invalid player tags: {tags}'.format(
            tags=", ".join(prepared["invalid_tags"]),
        ))
    if prepared["refresh_boards"]:
        lines.append(ACTION_ACCOUNT_BOARD)
    return tuple(lines)


async def prepare_account_add(context: Any,
                              values: Mapping[str, Any]) -> ChangePreview:
    workflow = _workflow(context)
    member = await _member(context, values["member"])
    if member is None:
        raise ValueError('That member is unavailable.')
    prepared = await workflow.account_add_operation(member, values["tags"], commit=False)
    if prepared["error"]:
        raise ValueError(prepared["error"])

    async def recheck() -> bool:
        if await _member(context, member.id) is None:
            return False
        current = await workflow.account_add_operation(member, values["tags"], commit=False)
        return (current["before"] == prepared["before"]
                and current["member_links"] == prepared["member_links"]
                and current["rows"] == prepared["rows"]
                and current["refresh_boards"] == prepared["refresh_boards"])

    return ChangePreview(
        _add_lines(prepared, member), recheck,
        summary=ACTION_ACCOUNT_ADD_LABEL, count=len(prepared["rows"]),
        before={"links": prepared["before"]},
    )


async def run_account_add(context: Any,
                          values: Mapping[str, Any]) -> ActionOutcome:
    workflow = _workflow(context)
    member = await _member(context, values["member"])
    if member is None:
        return ActionOutcome.unavailable()
    result = await workflow.account_add_operation(member, values["tags"])
    if result["error"]:
        return ActionOutcome.unavailable()
    tags = [row["player_tag"] for row in result["rows"]]
    after = workflow.get_links_by_tags(tags)
    if any(
        after.get(row["player_tag"]) is None
        or int(after[row["player_tag"]]["discord_user_id"]) != member.id
        or bool(after[row["player_tag"]]["is_primary"]) != bool(row["is_primary"])
        or after[row["player_tag"]]["player_name_last_seen"] != row["player_name_last_seen"]
        for row in result["rows"]
    ):
        raise OSError("Account links could not be verified")
    return ActionOutcome("complete", "private", text=result["message"],
                          after={"links": after})


async def prepare_account_add_undo(context: Any,
                                   log: Mapping[str, Any]) -> PreparedAction:
    workflow = _workflow(context)
    before = (log.get("before") or {}).get("links")
    expected = (log.get("after") or {}).get("links")
    if before is None or expected is None:
        raise ValueError("That account link change is unavailable")
    tags = tuple(before)
    current = workflow.get_links_by_tags(tags)
    refresh_boards = workflow.account_board_refresh_available()

    async def recheck() -> bool:
        return (workflow.get_links_by_tags(tags) == expected
                and workflow.account_board_refresh_available() == refresh_boards)

    async def run() -> ActionOutcome:
        await workflow.restore_account_add(before)
        after = workflow.get_links_by_tags(tags)
        if after != before:
            raise OSError("Account link undo could not be verified")
        return ActionOutcome("complete", "private", after={"links": after})

    lines = []
    for tag, old in before.items():
        if old is None:
            lines.append(ACTION_ACCOUNT_REMOVE_LINE.format(
                tag=tag, member=f"<@{int(expected[tag]['discord_user_id'])}>",
            ))
        else:
            lines.append(ACTION_ACCOUNT_ADD_LINE.format(
                name=old.get("player_name_last_seen") or tag, tag=tag,
                member=f"<@{int(old['discord_user_id'])}>",
            ))
    if refresh_boards:
        lines.append(ACTION_ACCOUNT_BOARD)
    if current != expected:
        lines.append(ACTION_UNDO_CHANGED)
    return PreparedAction(
        "undo_account_add", {"tags": tags},
        ChangePreview(tuple(lines), recheck,
                      summary=ACTION_ACCOUNT_ADD_UNDO_LABEL,
                      count=len(tags), before={"links": current}),
        run,
    )


async def prepare_account_remove(context: Any,
                                 values: Mapping[str, Any]) -> ChangePreview | ActionOutcome:
    workflow = _workflow(context)
    prepared = await workflow.account_remove_operation(values["tags"], commit=False)
    if prepared["error"]:
        raise ValueError(prepared["error"])
    if not prepared["tags_to_remove"]:
        return ActionOutcome("complete", "private", text=prepared["message"])
    lines = [ACTION_ACCOUNT_REMOVE_LINE.format(
        tag=tag, member=f"<@{int(prepared['before'][tag]['discord_user_id'])}>",
    ) for tag in prepared["tags_to_remove"]]
    lines.extend(ACTION_ACCOUNT_NO_LINK.format(tag=tag)
                 for tag, owner in prepared["before"].items() if owner is None)
    if prepared["invalid_tags"]:
        lines.append('Invalid player tags: {tags}'.format(
            tags=", ".join(prepared["invalid_tags"]),
        ))
    if prepared["refresh_boards"]:
        lines.append(ACTION_ACCOUNT_BOARD)

    async def recheck() -> bool:
        current = await workflow.account_remove_operation(values["tags"], commit=False)
        return (current["before"] == prepared["before"]
                and current["refresh_boards"] == prepared["refresh_boards"])

    return ChangePreview(tuple(lines), recheck,
                         summary=ACTION_ACCOUNT_REMOVE_LABEL,
                         count=len(prepared["tags_to_remove"]),
                         before={"links": prepared["before"]})


async def run_account_remove(context: Any,
                             values: Mapping[str, Any]) -> ActionOutcome:
    workflow = _workflow(context)
    result = await workflow.account_remove_operation(values["tags"])
    if result["error"]:
        return ActionOutcome.unavailable()
    after = workflow.get_links_by_tags(result["tags_to_remove"])
    if any(after.values()):
        raise OSError("Account unlink could not be verified")
    return ActionOutcome("complete", "private", text=result["message"],
                          after={"links": after})


def account_link_adapters() -> tuple[CommandAdapter, ...]:
    return (
        CommandAdapter("/account add", "confirm", run_account_add,
                       prepare=prepare_account_add),
        CommandAdapter("/account remove", "confirm", run_account_remove,
                       prepare=prepare_account_remove,
                       action_class=ActionClass.CHANGE),
    )


UNDO_HANDLERS = {
    '/account add': prepare_account_add_undo,
    'undo_account_add': prepare_account_add_undo,
}

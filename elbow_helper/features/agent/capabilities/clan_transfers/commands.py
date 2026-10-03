"""Clan transfer requests through the transfer queue feature."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.configuration.channels import TRANSFER_REQUESTS
from elbow_helper.features.clan_transfers.config import CLAN_TRANSFER_QUEUES

from ...actions.contracts import ActionRefused, ActionClass, ChangePreview
from ...wording import (
    ACTION_TRANSFER_BOARD,
    ACTION_TRANSFER_CANCEL_LABEL,
    ACTION_TRANSFER_CANCEL_LINE,
    ACTION_TRANSFER_COUNT,
    ACTION_TRANSFER_PING,
    ACTION_TRANSFER_REQUEST_LABEL,
    ACTION_TRANSFER_REQUEST_LINE,
)
from ...actions.outcomes import ActionOutcome
from ...commands.registry import CommandAdapter, PreparedCommandChange


async def _prepare(context: Any, values: Mapping[str, Any],
                   *, cancel: bool) -> PreparedCommandChange:
    workflow = context.bot.get_cog("ClanTransfers")
    if workflow is None:
        raise ActionRefused('That transfer queue is unavailable.')
    clan_code = values["destination"]
    if clan_code not in CLAN_TRANSFER_QUEUES:
        raise ActionRefused('That transfer queue is unavailable.')
    state = workflow.transfer_request_preview(
        clan_code, context.member.id, cancel=cancel,
    )
    if state["issue"]:
        raise ActionRefused(state["issue"])
    lines = [
        (ACTION_TRANSFER_CANCEL_LINE if cancel else ACTION_TRANSFER_REQUEST_LINE).format(
            clan=clan_code, member=context.member.mention,
        ),
        ACTION_TRANSFER_BOARD.format(
            thread=f"<#{state['thread_id']}>", board=f"<#{TRANSFER_REQUESTS}>",
        ),
        ACTION_TRANSFER_COUNT.format(
            count=state["pending_count"] - 1 if cancel else state["pending_count"] + 1,
        ),
    ]
    if state["notify_role_ids"]:
        lines.append(ACTION_TRANSFER_PING.format(
            roles=" ".join(f"<@&{role_id}>" for role_id in state["notify_role_ids"]),
            thread=f"<#{state['thread_id']}>",
        ))

    async def recheck() -> bool:
        return workflow.transfer_request_preview(
            clan_code, context.member.id, cancel=cancel,
        ) == state

    async def run() -> ActionOutcome:
        message = (await workflow.cancel_transfer(clan_code, context.member.id)
                   if cancel else
                   await workflow.request_transfer(clan_code, context.member.id))
        if workflow.transfer_request_status(clan_code, context.member.id) == cancel:
            raise OSError("Transfer request could not be verified")
        return ActionOutcome("complete", "private", text=message,
                              after={"clan_code": clan_code, "pending": not cancel})

    label = ACTION_TRANSFER_CANCEL_LABEL if cancel else ACTION_TRANSFER_REQUEST_LABEL
    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck, summary=label,
                      before={"clan_code": clan_code,
                              "pending": cancel}),
        run,
    )


async def prepare_transfer_request(context: Any,
                                   values: Mapping[str, Any]) -> PreparedCommandChange:
    return await _prepare(context, values, cancel=False)


async def prepare_transfer_cancel(context: Any,
                                  values: Mapping[str, Any]) -> PreparedCommandChange:
    return await _prepare(context, values, cancel=True)


async def run_transfer_request(context: Any,
                               values: Mapping[str, Any]) -> ActionOutcome:
    return await (await prepare_transfer_request(context, values)).run()


async def run_transfer_cancel(context: Any,
                              values: Mapping[str, Any]) -> ActionOutcome:
    return await (await prepare_transfer_cancel(context, values)).run()


def clan_transfer_adapters() -> tuple[CommandAdapter, ...]:
    return (
        CommandAdapter("/transfer request", "confirm", run_transfer_request,
                       prepare=prepare_transfer_request,
                       action_class=ActionClass.CHANGE),
        CommandAdapter("/transfer cancel", "confirm", run_transfer_cancel,
                       prepare=prepare_transfer_cancel,
                       action_class=ActionClass.CHANGE),
    )

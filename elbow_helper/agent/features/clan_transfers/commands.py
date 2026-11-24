"""Clan transfer requests through the transfer queue feature."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.features.clan_transfers.config import CLAN_TRANSFER_QUEUES

from elbow_helper.agent.actions.contracts import ActionRefused, ActionClass, ChangePreview
from elbow_helper.agent.conversation.wording import ACTION_TRANSFER_CANCEL_LABEL, ACTION_TRANSFER_CANCEL_LINE, ACTION_TRANSFER_PING, ACTION_TRANSFER_REQUEST_LABEL, ACTION_TRANSFER_REQUEST_LINE
from elbow_helper.agent.actions.outcomes import ActionOutcome
from elbow_helper.agent.commands.registry import CommandAdapter, PreparedCommandChange


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
        change = workflow.cancel_transfer if cancel else workflow.request_transfer
        await change(clan_code, context.member.id, via="agent")
        member = context.member.mention
        message = (f"Removed {member} from the {clan_code} transfer queue." if cancel
                   else f"{member} is in the {clan_code} transfer queue.")
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
                       action_class=ActionClass.CHANGE,
                       tool_description="Use /transfer request on the requester's behalf to join the destination clan's transfer queue."),
        CommandAdapter("/transfer cancel", "confirm", run_transfer_cancel,
                       prepare=prepare_transfer_cancel,
                       action_class=ActionClass.CHANGE,
                       tool_description="Use /transfer cancel on the requester's behalf to remove their request from the destination clan's queue."),
    )

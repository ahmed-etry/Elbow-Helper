"""Register a CWL thread through its feature operation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.features.agent.discord_actions.safety import (
    check_post_access,
)

from ...actions.contracts import ActionRefused, ActionClass, ChangePreview
from ...wording import (
    ACTION_CWL_REGISTER_LABEL,
    ACTION_CWL_REGISTER_LINE,
    ACTION_CWL_REGISTER_OLD,
    ACTION_CWL_REGISTER_CLOSED,
    ACTION_CWL_REGISTER_WELCOME,
    ACTION_CWL_REGISTER_BOARD,
)
from ...actions.outcomes import ActionOutcome, embed_text
from ...commands.registry import CommandAdapter


async def _registration(context: Any, values: Mapping[str, Any]):
    workflow = context.bot.get_cog("CwlManagement")
    if workflow is None:
        raise ActionRefused('CWL registration is unavailable.')
    prepared = await workflow.prepare_cwl_thread_registration(
        values["clan"], values["thread_id"],
    )
    if prepared["issue"]:
        raise ActionRefused(prepared["issue"])
    thread = prepared["thread"]
    if thread.guild.id != context.guild.id:
        raise ActionRefused('That thread is unavailable here.')
    if getattr(thread, "archived", False) or getattr(thread, "locked", False):
        raise ActionRefused(ACTION_CWL_REGISTER_CLOSED)
    check_post_access(thread, context.member, context.guild.me)
    if prepared["status"] == "new":
        prepared["status_preview"] = await workflow.cwl_registration_status_preview(
            prepared["clan"],
        )
        if prepared["status_preview"]["kind"] == "unavailable":
            raise ActionRefused('CWL status is unavailable right now.')
    return workflow, prepared


def _signature(prepared: Mapping[str, Any]):
    status = prepared.get("status_preview") or {}
    return (prepared["status"], prepared["thread"].id,
            prepared.get("clan_thread_id"), prepared.get("prior_data"),
            prepared.get("existing_thread_data"), status.get("kind"),
            embed_text(status["embed"]) if status.get("kind") == "board" else None)


async def prepare_cwl_register(context: Any,
                               values: Mapping[str, Any]) -> ChangePreview | ActionOutcome:
    workflow, prepared = await _registration(context, values)
    if prepared["status"] == "already":
        return ActionOutcome("complete", "private", text=prepared["message"])
    thread = prepared["thread"]
    lines = [ACTION_CWL_REGISTER_LINE.format(
        thread=thread.mention, clan=prepared["clan"],
    )]
    old_ids = set(prepared["prior_threads"])
    if (prepared["clan_thread_id"] and
            prepared["clan_thread_id"] != thread.id):
        old_ids.add(str(prepared["clan_thread_id"]))
    lines.extend(ACTION_CWL_REGISTER_OLD.format(thread_id=thread_id)
                 for thread_id in sorted(old_ids))
    welcome = workflow.cwl_registration_welcome_embed(prepared["clan"])
    lines.append(ACTION_CWL_REGISTER_WELCOME.format(thread=thread.mention))
    details = list(embed_text(welcome).splitlines())
    status = prepared["status_preview"]
    if status["kind"] == "board":
        lines.append(ACTION_CWL_REGISTER_BOARD.format(thread=thread.mention))
        details.extend(embed_text(status["embed"]).splitlines())
    signature = _signature(prepared)

    async def recheck() -> bool:
        try:
            current, live = await _registration(context, values)
        except ValueError:
            return False
        return current is workflow and _signature(live) == signature

    return ChangePreview(tuple(lines), recheck,
                         summary=ACTION_CWL_REGISTER_LABEL, details=tuple(details),
                         before={"clan": prepared["clan"],
                                 "prior_threads": prepared["prior_data"],
                                 "clan_thread_id": prepared["clan_thread_id"]})


async def run_cwl_register(context: Any,
                           values: Mapping[str, Any]) -> ActionOutcome:
    workflow, prepared = await _registration(context, values)
    message = await workflow.apply_cwl_thread_registration(prepared)
    after = await workflow.prepare_cwl_thread_registration(
        values["clan"], values["thread_id"],
    )
    if after["issue"] or after["status"] != "already":
        raise OSError("CWL thread registration could not be verified")
    return ActionOutcome("complete", "private", text=message,
                          after={"thread_id": prepared["thread"].id,
                                 "clan": prepared["clan"]})


def cwl_register_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter("/cwl register", "confirm", run_cwl_register,
                           prepare=prepare_cwl_register,
                           action_class=ActionClass.CHANGE),)

"""Leadership record commands through their public feature operations."""

from __future__ import annotations

import asyncio
import zipfile
from collections.abc import Mapping
from typing import Any
from xml.etree import ElementTree

import discord

from elbow_helper.features.agent.access import ACCESS_LEAD_PLUS, has_access_requirements
from elbow_helper.features.records.commands.records import open_record_editor
from elbow_helper.features.records.domain.types import category_label, incident_type_label

from ...actions.contracts import ActionClass, ChangePreview, PreparedAction
from ...models import AgentAttachment
from ...wording import (
    ACTION_RECORD_ADD_LABEL, ACTION_RECORD_ADD_LINE, ACTION_RECORD_ADD_UNDO,
    ACTION_RECORD_ADD_UNDO_DONE, ACTION_RECORD_DETAILS_LINE,
    ACTION_RECORD_REMOVE_DONE, ACTION_RECORD_REMOVE_LABEL,
    ACTION_RECORD_REMOVE_LINE, ACTION_RECORD_UNDO_LABEL, ACTION_UNDO_CHANGED,
    COMMAND_UNAVAILABLE,
)
from ..outcomes import CommandOutcome
from ..registry import CommandAdapter


def _workflow(context: Any) -> Any | None:
    if not has_access_requirements(
        context.guild, context.member.id, {ACCESS_LEAD_PLUS},
    ):
        return None
    context.state.required_access.add(ACCESS_LEAD_PLUS)
    return context.bot.get_cog("Records")


async def _member(context: Any, member_id: int) -> Any | None:
    member = context.guild.get_member(member_id)
    if member is not None:
        return member
    try:
        return await context.guild.fetch_member(member_id)
    except discord.DiscordException:
        return None


def _record_signature(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(record.get(key) for key in (
        "id", "member_id", "status", "category_key", "incident_type_key",
        "note", "updated_ts",
    ))


async def prepare_record_add(context: Any, values: Mapping[str, Any]) -> ChangePreview:
    workflow = _workflow(context)
    if workflow is None:
        raise ValueError("Leadership records are unavailable")
    member = await _member(context, values["user"])
    if member is None:
        raise ValueError("That member is unavailable")
    category, incident, note = workflow.service.validate_details(
        values["category"], values["type"], values["note"],
    )

    async def recheck() -> bool:
        return _workflow(context) is not None and await _member(context, member.id) is not None

    return ChangePreview((
        ACTION_RECORD_ADD_LINE.format(
            category=category_label(category), incident=incident_type_label(incident),
            member=member.mention,
        ), ACTION_RECORD_DETAILS_LINE.format(note=note),
    ), recheck, summary=ACTION_RECORD_ADD_LABEL, before={"record": None})


async def run_record_add(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
    workflow = _workflow(context)
    if workflow is None:
        return CommandOutcome.unavailable()
    member = await _member(context, values["user"])
    if member is None:
        return CommandOutcome.unavailable()
    try:
        record = await asyncio.to_thread(
            workflow.service.create,
            member=member, category_key=values["category"],
            incident_type_key=values["type"], note=values["note"],
            recorder=context.member,
        )
    except ValueError:
        return CommandOutcome.unavailable()
    verified = await asyncio.to_thread(
        workflow.service.active_record,
        member_id=member.id, record_id=record["id"],
    )
    if verified is None:
        raise OSError("Record creation could not be verified")
    return CommandOutcome(
        "complete", "private", text=workflow.service.confirmation(record),
        after={"record_id": record["id"], "member_id": member.id},
        result={"record_id": record["id"], "member_id": member.id},
    )


async def run_record_export(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
    workflow = _workflow(context)
    if workflow is None:
        return CommandOutcome.unavailable()
    member = await _member(context, values["user"]) if values.get("user") else None
    if values.get("user") and member is None:
        return CommandOutcome.unavailable()
    try:
        report = await workflow.exports.create(
            member_id=member.id if member else None,
            member_name=workflow.service.display_name(member) if member else None,
        )
    except (OSError, TypeError, ValueError, zipfile.BadZipFile,
            ElementTree.ParseError):
        return CommandOutcome.unavailable()
    try:
        if report.google_link:
            return CommandOutcome("complete", "private",
                                  private_parts=(report.google_link,))
        data = await asyncio.to_thread(report.workbook_path.read_bytes)
        return CommandOutcome(
            "complete", "private",
            private_parts=((report.google_warning,) if report.google_warning else ()),
            attachments=(AgentAttachment(report.workbook_name, data),),
        )
    finally:
        await workflow.exports.discard(report)


async def run_record_edit(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
    workflow = _workflow(context)
    if workflow is None:
        return CommandOutcome.unavailable()
    member = await _member(context, values["user"])
    if member is None:
        return CommandOutcome.unavailable()
    records = await asyncio.to_thread(workflow.service.edit_options, member_id=member.id)
    if not records:
        return CommandOutcome("empty", "private")

    async def open_panel(interaction):
        if _workflow(context) is None:
            await interaction.response.send_message(
                COMMAND_UNAVAILABLE, ephemeral=True,
            )
            return
        await open_record_editor(
            interaction, service=workflow.service, member=member, records=records,
        )

    return CommandOutcome("complete", "private", private_panel=open_panel)


async def prepare_record_remove(context: Any, values: Mapping[str, Any]) -> ChangePreview:
    workflow = _workflow(context)
    if workflow is None:
        raise ValueError("Leadership records are unavailable")
    member = await _member(context, values["user"])
    if member is None:
        raise ValueError("That member is unavailable")
    record_id = int(values["record"])
    record = await asyncio.to_thread(
        workflow.service.active_record,
        member_id=member.id, record_id=record_id,
    )
    if record is None:
        raise ValueError("That record is unavailable")
    signature = _record_signature(record)

    async def recheck() -> bool:
        if _workflow(context) is None:
            return False
        current = await asyncio.to_thread(
            workflow.service.active_record,
            member_id=member.id, record_id=record_id,
        )
        return current is not None and _record_signature(current) == signature

    return ChangePreview((
        ACTION_RECORD_REMOVE_LINE.format(record_id=record_id, member=member.mention),
        ACTION_RECORD_DETAILS_LINE.format(note=record["note"]),
    ), recheck, summary=ACTION_RECORD_REMOVE_LABEL,
        before={"record": record})


async def run_record_remove(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
    workflow = _workflow(context)
    if workflow is None:
        return CommandOutcome.unavailable()
    member = await _member(context, values["user"])
    if member is None:
        return CommandOutcome.unavailable()
    record_id = int(values["record"])
    removed = await asyncio.to_thread(
        workflow.service.remove,
        record_id=record_id, member_id=member.id, remover=context.member,
    )
    if removed is None:
        return CommandOutcome.unavailable()
    current = await asyncio.to_thread(
        workflow.service.active_record,
        member_id=member.id, record_id=record_id,
    )
    if current is not None:
        return CommandOutcome.unavailable()
    return CommandOutcome(
        "complete", "private",
        text=ACTION_RECORD_REMOVE_DONE.format(
            record_id=record_id, member=member.display_name,
        ),
        after={"removed": True},
    )


async def prepare_record_add_undo(context: Any,
                                  log: Mapping[str, Any]) -> PreparedAction:
    workflow = _workflow(context)
    if workflow is None or log["after"] is None:
        raise ValueError("That record change is unavailable")
    record_id = log["after"]["record_id"]
    member_id = log["after"]["member_id"]
    record = await asyncio.to_thread(
        workflow.service.active_record, member_id=member_id, record_id=record_id,
    )
    if record is None:
        raise ValueError("That record is unavailable")
    member = await _member(context, member_id)
    if member is None:
        raise ValueError("That member is unavailable")
    expected = (record["category_key"], record["incident_type_key"], record["note"])
    original = workflow.service.validate_details(
        log["targets"]["category"], log["targets"]["type"],
        log["targets"]["note"],
    )
    changed = expected != original

    async def recheck() -> bool:
        current = await asyncio.to_thread(
            workflow.service.active_record,
            member_id=member_id, record_id=record_id,
        )
        return (_workflow(context) is not None and current is not None and
                (current["category_key"], current["incident_type_key"],
                 current["note"]) == original)

    async def run() -> CommandOutcome:
        removed = await asyncio.to_thread(
            workflow.service.remove,
            record_id=record_id, member_id=member_id, remover=context.member,
        )
        if removed is None:
            return CommandOutcome.unavailable()
        return CommandOutcome(
            "complete", "private", text=ACTION_RECORD_ADD_UNDO_DONE.format(
                record_id=record_id, member=member.mention,
            ), after={"removed": True},
        )

    return PreparedAction(
        "undo_record_add", {"record_id": record_id, "member_id": member_id},
        ChangePreview((
            ACTION_RECORD_ADD_UNDO.format(record_id=record_id, member=member.mention),
            *((ACTION_UNDO_CHANGED,) if changed else ()),
        ), recheck, summary=ACTION_RECORD_UNDO_LABEL,
            before={"record": record}),
        run,
    )


def record_adapters() -> tuple[CommandAdapter, ...]:
    return (
        CommandAdapter("/record add", "confirm", run_record_add,
                       prepare=prepare_record_add),
        CommandAdapter("/record export", "private", run_record_export),
        CommandAdapter("/record edit", "private", run_record_edit),
        CommandAdapter("/record remove", "confirm", run_record_remove,
                       prepare=prepare_record_remove,
                       action_class=ActionClass.IRREVERSIBLE,
                       option_types=(("record", "integer"),)),
    )

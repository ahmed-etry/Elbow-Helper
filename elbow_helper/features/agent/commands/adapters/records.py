"""Leadership record commands through their public feature operations."""

from __future__ import annotations

import asyncio
import zipfile
from collections.abc import Mapping
from typing import Any
from xml.etree import ElementTree

import discord

from elbow_helper.features.agent.access import ACCESS_LEAD_PLUS, has_access_requirements
from elbow_helper.features.help.discovery import ParameterInfo
from elbow_helper.features.records.domain.types import (
    RECORD_CATEGORIES, category_label, incident_type_label,
)

from ...actions.contracts import ActionClass, ChangePreview, PreparedAction
from ...models import AgentAttachment
from ...wording import (
    ACTION_RECORD_ADD_LABEL, ACTION_RECORD_ADD_LINE, ACTION_RECORD_ADD_UNDO,
    ACTION_RECORD_ADD_UNDO_DONE, ACTION_RECORD_DETAILS_LINE,
    ACTION_RECORD_EDIT_CATEGORY_LINE, ACTION_RECORD_EDIT_CATEGORY_OPTION,
    ACTION_RECORD_EDIT_INPUT, ACTION_RECORD_EDIT_LABEL, ACTION_RECORD_EDIT_LINE,
    ACTION_RECORD_EDIT_NO_CHANGE, ACTION_RECORD_EDIT_NOTE_LINE,
    ACTION_RECORD_EDIT_NOTE_OPTION, ACTION_RECORD_EDIT_RECORD_OPTION,
    ACTION_RECORD_EDIT_TYPE_INPUT, ACTION_RECORD_EDIT_TYPE_LINE,
    ACTION_RECORD_EDIT_TYPE_OPTION, ACTION_RECORD_EDIT_UNDO_LABEL,
    ACTION_RECORD_REMOVE_DONE, ACTION_RECORD_REMOVE_LABEL,
    ACTION_RECORD_REMOVE_LINE, ACTION_RECORD_UNDO_LABEL, ACTION_UNDO_CHANGED,
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


def _edit_target(service: Any, record: Mapping[str, Any],
                 values: Mapping[str, Any]) -> tuple[str, str, str]:
    return service.validate_details(
        values.get("category", record["category_key"]),
        values.get("type", record["incident_type_key"]),
        values.get("note", record["note"]),
    )


def _edit_lines(record: Mapping[str, Any], target: tuple[str, str, str],
                member: Any) -> tuple[str, ...]:
    old = (record["category_key"], record["incident_type_key"], record["note"])
    templates = (
        (ACTION_RECORD_EDIT_CATEGORY_LINE, category_label),
        (ACTION_RECORD_EDIT_TYPE_LINE, incident_type_label),
        (ACTION_RECORD_EDIT_NOTE_LINE, str),
    )
    return (ACTION_RECORD_EDIT_LINE.format(
        record_id=record["id"], member=member.mention,
    ), *(template.format(old=label(previous), new=label(updated))
          for previous, updated, (template, label) in zip(old, target, templates)
          if previous != updated))


async def prepare_record_edit(context: Any, values: Mapping[str, Any]) -> ChangePreview | CommandOutcome:
    workflow = _workflow(context)
    if workflow is None:
        raise ValueError("Leadership records are unavailable")
    member = await _member(context, values["user"])
    if member is None:
        raise ValueError("That member is unavailable")
    records = await asyncio.to_thread(workflow.service.edit_options, member_id=member.id)
    record_id = int(values["record"])
    record = next((item for item in records if item["id"] == record_id), None)
    if record is None:
        raise ValueError("That record is unavailable")
    if not any(key in values for key in ("category", "type", "note")):
        return CommandOutcome.needs_input((ACTION_RECORD_EDIT_INPUT,))
    if (values.get("category", record["category_key"]) != record["category_key"]
            and "type" not in values):
        return CommandOutcome.needs_input((ACTION_RECORD_EDIT_TYPE_INPUT,))
    target = _edit_target(workflow.service, record, values)
    if target == (record["category_key"], record["incident_type_key"], record["note"]):
        return CommandOutcome("complete", "private", text=ACTION_RECORD_EDIT_NO_CHANGE)
    signature = _record_signature(record)

    async def recheck() -> bool:
        if _workflow(context) is None:
            return False
        current = await asyncio.to_thread(
            workflow.service.active_record,
            member_id=member.id, record_id=record_id,
        )
        return current is not None and _record_signature(current) == signature

    return ChangePreview(
        _edit_lines(record, target, member), recheck,
        summary=ACTION_RECORD_EDIT_LABEL, before={"record": record},
    )


async def run_record_edit(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
    workflow = _workflow(context)
    if workflow is None:
        return CommandOutcome.unavailable()
    member = await _member(context, values["user"])
    if member is None:
        return CommandOutcome.unavailable()
    record_id = int(values["record"])
    old = await asyncio.to_thread(
        workflow.service.active_record, member_id=member.id, record_id=record_id,
    )
    if old is None:
        return CommandOutcome.unavailable()
    target = _edit_target(workflow.service, old, values)
    edited = await asyncio.to_thread(
        workflow.service.edit,
        record_id=record_id, member_id=member.id,
        category_key=target[0], incident_type_key=target[1], note=target[2],
        editor=context.member,
    )
    if edited is None:
        return CommandOutcome.unavailable()
    verified = await asyncio.to_thread(
        workflow.service.active_record, member_id=member.id, record_id=record_id,
    )
    if verified is None or (verified["category_key"], verified["incident_type_key"],
                            verified["note"]) != target:
        raise OSError("Record edit could not be verified")
    return CommandOutcome(
        "complete", "private",
        text=workflow.service.edit_confirmation(record_id, member),
        after={"record": verified}, result={"record_id": record_id, "member_id": member.id},
    )


async def prepare_record_edit_undo(context: Any,
                                   log: Mapping[str, Any]) -> PreparedAction:
    workflow = _workflow(context)
    before = (log.get("before") or {}).get("record")
    after = (log.get("after") or {}).get("record")
    if workflow is None or before is None or after is None:
        raise ValueError("That record change is unavailable")
    member_id = int(before["member_id"])
    record_id = int(before["id"])
    member = await _member(context, member_id)
    if member is None:
        raise ValueError("That member is unavailable")
    current = await asyncio.to_thread(
        workflow.service.active_record, member_id=member_id, record_id=record_id,
    )
    if current is None:
        raise ValueError("That record is unavailable")
    target = (before["category_key"], before["incident_type_key"], before["note"])
    signature = _record_signature(after)
    changed = _record_signature(current) != signature

    async def recheck() -> bool:
        if _workflow(context) is None:
            return False
        live = await asyncio.to_thread(
            workflow.service.active_record, member_id=member_id, record_id=record_id,
        )
        return live is not None and _record_signature(live) == signature

    async def run() -> CommandOutcome:
        restored = await asyncio.to_thread(
            workflow.service.edit,
            record_id=record_id, member_id=member_id,
            category_key=target[0], incident_type_key=target[1], note=target[2],
            editor=context.member,
        )
        if restored is None:
            return CommandOutcome.unavailable()
        verified = await asyncio.to_thread(
            workflow.service.active_record, member_id=member_id, record_id=record_id,
        )
        if verified is None or (verified["category_key"], verified["incident_type_key"],
                                verified["note"]) != target:
            raise OSError("Record edit undo could not be verified")
        return CommandOutcome("complete", "private", after={"record": verified})

    return PreparedAction(
        "undo_record_edit", {"member_id": member_id, "record_id": record_id},
        ChangePreview((
            *_edit_lines(current, target, member),
            *((ACTION_UNDO_CHANGED,) if changed else ()),
        ), recheck, summary=ACTION_RECORD_EDIT_UNDO_LABEL,
            before={"record": current}),
        run,
    )


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
        CommandAdapter(
            "/record edit", "confirm", run_record_edit,
            prepare=prepare_record_edit,
            options=(
                ParameterInfo("record", ACTION_RECORD_EDIT_RECORD_OPTION, True, "integer"),
                ParameterInfo(
                    "category", ACTION_RECORD_EDIT_CATEGORY_OPTION, False, "string",
                    tuple(item.label for item in RECORD_CATEGORIES), False,
                    tuple(item.key for item in RECORD_CATEGORIES),
                ),
                ParameterInfo("type", ACTION_RECORD_EDIT_TYPE_OPTION, False, "string"),
                ParameterInfo("note", ACTION_RECORD_EDIT_NOTE_OPTION, False, "string"),
            ),
            agent_details=(
                "Choose a record and the values to change. The agent shows the old and "
                "new values before applying the edit."
            ),
        ),
        CommandAdapter("/record remove", "confirm", run_record_remove,
                       prepare=prepare_record_remove,
                       action_class=ActionClass.IRREVERSIBLE,
                       option_types=(("record", "integer"),)),
    )

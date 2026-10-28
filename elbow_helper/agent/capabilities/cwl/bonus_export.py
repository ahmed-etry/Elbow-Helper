"""CWL bonus report command adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from elbow_helper.features.cwl.bonus.commands import bonus_report_lines
from elbow_helper.features.cwl.bonus.service import BonusReportError

from ...models import AgentAttachment
from ...actions.outcomes import ActionOutcome
from ...commands.registry import CommandAdapter


async def run_cwl_bonus(context: Any, values: Mapping[str, Any]) -> ActionOutcome:
    workflow = context.bot.get_cog("CwlManagement")
    if workflow is None:
        return ActionOutcome.unavailable()
    try:
        report = await workflow.bonus_reports.create(values["clan"], values.get("season"))
    except BonusReportError as error:
        return ActionOutcome(
            "complete", text=workflow.bonus_report_error_message(error),
        )
    try:
        if report.google_link:
            return ActionOutcome(
                "complete", text="\n".join((
                    f"**CWL Bonus Estimate ({report.scope_label}, {report.season})**",
                    report.google_link,
                )),
            )
        data = await asyncio.to_thread(report.workbook_path.read_bytes)
        return ActionOutcome(
            "complete", text="\n".join(bonus_report_lines(report)),
            attachments=(AgentAttachment(report.workbook_name, data),),
        )
    finally:
        await workflow.bonus_reports.discard(report)


def cwl_bonus_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter("/cwl bonus", "public", run_cwl_bonus),)

"""CWL bonus report command adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from elbow_helper.features.cwl.bonus.commands import bonus_report_lines
from elbow_helper.features.cwl.bonus.service import BonusReportError

from ...models import AgentAttachment
from ..outcomes import CommandOutcome
from ..registry import CommandAdapter


async def run_cwl_bonus(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
    workflow = context.bot.get_cog("CwlManagement")
    if workflow is None:
        return CommandOutcome.unavailable()
    try:
        report = await workflow.bonus_reports.create(values["clan"], values.get("season"))
    except BonusReportError as error:
        return CommandOutcome(
            "complete", text=workflow.bonus_report_error_message(error),
        )
    try:
        if report.google_link:
            return CommandOutcome(
                "complete", text="\n".join((
                    f"**CWL Bonus Estimate ({report.scope_label}, {report.season})**",
                    report.google_link,
                )),
            )
        data = await asyncio.to_thread(report.workbook_path.read_bytes)
        return CommandOutcome(
            "complete", text="\n".join(bonus_report_lines(report)),
            attachments=(AgentAttachment(report.workbook_name, data),),
        )
    finally:
        await workflow.bonus_reports.discard(report)


def cwl_bonus_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter("/cwl bonus", "public", run_cwl_bonus),)

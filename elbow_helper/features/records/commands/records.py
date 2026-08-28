"""Slash commands for leadership records."""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timezone as dt_timezone
import re
import zipfile
from xml.etree import ElementTree

import discord
from discord import app_commands

from elbow_helper.discord.interactions import deny
from elbow_helper.discord.interactions import warn
from elbow_helper.discord.interactions import send_bound_view
from elbow_helper.configuration.roles import LEAD_PLUS

from ..domain.types import CATEGORY_CHOICES
from ..domain.types import incident_type_label
from ..domain.types import incident_types_for_category

UTC = dt_timezone.utc


def _namespace_value(value: object) -> str:
    return str(getattr(value, "value", value) or "")


async def incident_type_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    category_key = _namespace_value(getattr(interaction.namespace, "category", ""))
    query = current.casefold().strip()
    choices = []
    for incident in incident_types_for_category(category_key):
        if query and query not in incident.label.casefold():
            continue
        choices.append(app_commands.Choice(name=incident.label, value=incident.key))
    return choices[:25]


class RecordCommandMixin:
    @staticmethod
    def _has_record_access(interaction: discord.Interaction) -> bool:
        return any(getattr(role, "id", None) in LEAD_PLUS for role in getattr(interaction.user, "roles", []))

    @app_commands.choices(category=CATEGORY_CHOICES)
    @app_commands.autocomplete(type=incident_type_autocomplete)
    @app_commands.describe(
        user="Member involved in the incident.",
        category="Broad category for the incident.",
        type="Kind of incident to record.",
        note="What happened and any context leadership should know.",
    )
    async def record_add(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        category: app_commands.Choice[str],
        type: str,
        note: str,
    ) -> None:
        if not self._has_record_access(interaction):
            await deny(interaction)
            return
        try:
            record = await asyncio.to_thread(
                self.service.create,
                member=user,
                category_key=category.value,
                incident_type_key=type,
                note=note.strip(),
                recorder=interaction.user,
            )
        except ValueError as exc:
            await warn(interaction, str(exc))
            return
        await interaction.response.send_message(
            self.service.confirmation(record),
            ephemeral=True,
        )

    @app_commands.describe(user="Member whose records you want, or leave empty for all records.")
    async def record_export(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        if not self._has_record_access(interaction):
            await deny(interaction)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            report = await self.exports.create(
                member_id=user.id if user else None,
                member_name=(
                    self.service.display_name(user)
                    if user
                    else None
                ),
            )
        except (
            OSError,
            TypeError,
            ValueError,
            zipfile.BadZipFile,
            ElementTree.ParseError,
        ):
            await interaction.followup.send(
                "I couldn't create the records spreadsheet.",
                ephemeral=True,
            )
            return
        await self._send_record_export(interaction, report)

    @app_commands.describe(user="Member whose records you want to edit.")
    async def record_edit(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not self._has_record_access(interaction):
            await deny(interaction)
            return
        records = await asyncio.to_thread(
            self.reader.list,
            member_id=user.id,
            limit=25,
        )
        if not records:
            await warn(interaction, f"No records found for {user.display_name}.")
            return
        from ..ui.edit_record import RecordEditView

        view = RecordEditView(
            self.service,
            member=user,
            records=records,
            owner_id=interaction.user.id,
        )
        await send_bound_view(interaction, embed=view.build_embed(), view=view, ephemeral=True)

    async def record_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not self._has_record_access(interaction):
            return []
        member = getattr(interaction.namespace, "user", None)
        member_id = getattr(member, "id", None)
        if not isinstance(member_id, int) or member_id <= 0:
            return []
        query = current.casefold().strip()
        choices: list[app_commands.Choice[str]] = []
        records = await asyncio.to_thread(
            self.reader.list,
            member_id=member_id,
        )
        for record in records:
            created = datetime.fromtimestamp(
                int(record.get("created_ts") or 0),
                tz=UTC,
            ).strftime("%Y-%m-%d")
            incident = incident_type_label(
                str(record.get("incident_type_key") or "")
            )
            label = f"#{record['id']} | {created} | {incident}"
            if query and query not in label.casefold():
                continue
            choices.append(
                app_commands.Choice(
                    name=label[:100],
                    value=str(record["id"]),
                )
            )
            if len(choices) >= 25:
                break
        return choices

    @app_commands.autocomplete(record=record_autocomplete)
    @app_commands.describe(
        user="Member the record belongs to.",
        record="Incident record to remove.",
    )
    async def record_remove(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        record: str,
    ) -> None:
        if not self._has_record_access(interaction):
            await deny(interaction)
            return
        try:
            record_id = int(record)
        except (TypeError, ValueError):
            await warn(interaction, "Choose a record from the list.")
            return
        removed = await asyncio.to_thread(
            self.service.remove,
            record_id=record_id,
            member_id=user.id,
            remover=interaction.user,
        )
        if removed is None:
            await warn(interaction, "That record is no longer available for this member.")
            return
        await interaction.response.send_message(
            f"Removed record #{record_id} for {user.display_name}.", ephemeral=True
        )

    @staticmethod
    async def _send_record_export(
        interaction: discord.Interaction,
        report,
    ) -> None:
        view = discord.ui.View(timeout=None)
        google_download_link = None
        if report.google_link:
            sheet_match = re.search(
                r"/spreadsheets/d/([a-zA-Z0-9-_]+)",
                report.google_link,
            )
            if sheet_match:
                google_download_link = (
                    "https://docs.google.com/spreadsheets/d/"
                    f"{sheet_match.group(1)}/export?format=xlsx"
                )
            view.add_item(
                discord.ui.Button(
                    label="Google Sheet",
                    style=discord.ButtonStyle.link,
                    url=report.google_link,
                )
            )
        if google_download_link:
            view.add_item(
                discord.ui.Button(
                    label="Download",
                    style=discord.ButtonStyle.link,
                    url=google_download_link,
                )
            )

        lines = ["**Leadership Records Export**"]
        if report.google_link and view.children:
            await interaction.followup.send(
                "\n".join(lines),
                view=view,
                ephemeral=True,
            )
            return

        if report.google_warning:
            lines.append(report.google_warning)
        message = await interaction.followup.send(
            "\n".join(lines),
            wait=True,
            file=discord.File(
                str(report.workbook_path),
                filename=report.workbook_name,
            ),
            ephemeral=True,
        )
        if message.attachments:
            view.add_item(
                discord.ui.Button(
                    label="Download",
                    style=discord.ButtonStyle.link,
                    url=message.attachments[0].url,
                )
            )
            await message.edit(content="\n".join(lines), view=view)

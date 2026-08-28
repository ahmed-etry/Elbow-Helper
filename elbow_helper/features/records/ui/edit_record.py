"""Interactive editor for a member's leadership records."""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any

import discord

from elbow_helper.discord.views import BaseErrorModal
from elbow_helper.discord.views import BaseTimeoutView
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL

from ..domain.types import RECORD_CATEGORIES
from ..domain.types import category_label
from ..domain.types import incident_type_label
from ..domain.types import incident_types_for_category

UTC = dt_timezone.utc


class RecordSelect(discord.ui.Select):
    def __init__(self, view: "RecordEditView"):
        options = []
        for record in view.records:
            created = datetime.fromtimestamp(int(record.get("created_ts") or 0), tz=UTC).strftime("%Y-%m-%d")
            record_id = int(record["id"])
            options.append(discord.SelectOption(
                label=f"#{record_id} | {incident_type_label(str(record.get('incident_type_key') or ''))}"[:100],
                value=str(record_id),
                description=created,
                default=view.selected_id is not None and record_id == view.selected_id,
            ))
        super().__init__(placeholder="Choose a record", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RecordEditView):
            return
        view.select_record(int(self.values[0]))
        await view.refresh(interaction)


class CategorySelect(discord.ui.Select):
    def __init__(self, current: str):
        options = [
            discord.SelectOption(
                label=category.label,
                value=category.key,
                default=category.key == current,
            )
            for category in RECORD_CATEGORIES
        ]
        super().__init__(placeholder="Category", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RecordEditView):
            return
        view.category_key = self.values[0]
        valid_types = incident_types_for_category(view.category_key)
        if not any(item.key == view.incident_type_key for item in valid_types):
            view.incident_type_key = valid_types[0].key
        await view.refresh(interaction)


class IncidentTypeSelect(discord.ui.Select):
    def __init__(self, category_key: str, current: str):
        options = [
            discord.SelectOption(
                label=incident.label,
                value=incident.key,
                default=incident.key == current,
            )
            for incident in incident_types_for_category(category_key)
        ]
        super().__init__(placeholder="Incident type", options=options, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RecordEditView):
            return
        view.incident_type_key = self.values[0]
        await view.refresh(interaction)


class NoteModal(BaseErrorModal, title="Edit Record Details"):
    def __init__(self, view: "RecordEditView"):
        super().__init__(timeout=300)
        self.view_ref = view
        self.note_input = discord.ui.TextInput(
            label="Details",
            style=discord.TextStyle.paragraph,
            default=view.note[:1000],
            required=True,
            max_length=1000,
        )
        self.add_item(self.note_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.view_ref.note = str(self.note_input.value or "").strip()
        await self.view_ref.refresh(interaction)


class EditNoteButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Edit Details", style=discord.ButtonStyle.secondary, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, RecordEditView):
            await interaction.response.send_modal(NoteModal(view))


class SaveButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Save", style=discord.ButtonStyle.primary, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RecordEditView):
            return
        try:
            updated = await asyncio.to_thread(
                view.service.edit,
                record_id=view.selected_id,
                member_id=view.member.id,
                category_key=view.category_key,
                incident_type_key=view.incident_type_key,
                note=view.note,
                editor=interaction.user,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if updated is None:
            await interaction.response.send_message("This record is no longer available.", ephemeral=True)
            return
        for child in view.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        await interaction.response.edit_message(
            content=f"Updated record #{view.selected_id} for {view.member.display_name}.",
            embed=None,
            view=view,
        )


class CancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RecordEditView):
            return
        for child in view.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        await interaction.response.edit_message(content="Record edit cancelled.", embed=None, view=view)


class RecordEditView(BaseTimeoutView):
    def __init__(self, service: Any, *, member: discord.Member, records: list[dict[str, Any]], owner_id: int):
        super().__init__(timeout=600)
        self.service = service
        self.member = member
        self.records = records
        self.owner_id = int(owner_id)
        self.selected_id: int | None = None
        self.category_key = ""
        self.incident_type_key = ""
        self.note = ""
        self._rebuild_items()

    def _selected_record(self) -> dict[str, Any]:
        if self.selected_id is None:
            raise LookupError("Choose a record first.")
        return next(record for record in self.records if int(record["id"]) == self.selected_id)

    def select_record(self, record_id: int) -> None:
        self.selected_id = int(record_id)
        record = self._selected_record()
        self.category_key = str(record.get("category_key") or "")
        self.incident_type_key = str(record.get("incident_type_key") or "")
        self.note = str(record.get("note") or "")
        self._rebuild_items()

    def _rebuild_items(self) -> None:
        self.clear_items()
        self.add_item(RecordSelect(self))
        if self.selected_id is None:
            return
        self.add_item(CategorySelect(self.category_key))
        self.add_item(IncidentTypeSelect(self.category_key, self.incident_type_key))
        self.add_item(EditNoteButton())
        self.add_item(SaveButton())
        self.add_item(CancelButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("This record editor was opened by someone else.", ephemeral=True)
        return False

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"Edit Record - {self.member.display_name}",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        if self.selected_id is None:
            embed.description = "Select a record to edit."
            return embed
        embed.add_field(name="Record", value=f"#{self.selected_id}", inline=True)
        embed.add_field(name="Category", value=category_label(self.category_key), inline=True)
        embed.add_field(name="Type", value=incident_type_label(self.incident_type_key), inline=True)
        embed.add_field(name="Details", value=self.note or "No details", inline=False)
        return embed

    async def refresh(self, interaction: discord.Interaction) -> None:
        self._rebuild_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

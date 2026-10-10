"""Private delivery for command results."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord

from ..wording import (
    ACTION_PRIVATE_BUTTON,
    ACTION_PRIVATE_SELECT, ACTION_RESULT_EXPIRED,
    ACTION_RESULT_OWNER,
)
from ..models import AgentAttachment
from ..files.delivery import attachment_files, spreadsheet_links, spreadsheet_warnings
from ..text import chunk_response


LOGGER = logging.getLogger(__name__)
PRIVATE_RESULT_TIMEOUT = 600.0


class PrivatePanelSelect(discord.ui.View):
    def __init__(self, owner_id: int,
                 panels: tuple[Callable[[discord.Interaction], Awaitable[None]], ...],
                 labels: tuple[str, ...]):
        super().__init__(timeout=PRIVATE_RESULT_TIMEOUT)
        if len(labels) != len(panels) or any(not label for label in labels):
            raise ValueError("Every private panel needs its command name")
        self.owner_id = owner_id
        self.panels = panels
        for start in range(0, len(panels), 25):
            selector = discord.ui.Select(
                placeholder=ACTION_PRIVATE_SELECT,
                options=[discord.SelectOption(
                    label=labels[index][:100],
                    value=str(index),
                ) for index in range(start, min(start + 25, len(panels)))],
            )
            async def choose(interaction, selected=selector):
                if interaction.user.id != self.owner_id:
                    await interaction.response.send_message(ACTION_RESULT_OWNER, ephemeral=True)
                    return
                await self.panels[int(selected.values[0])](interaction)
            selector.callback = choose
            self.add_item(selector)


class PrivateResultView(discord.ui.View):
    def __init__(self, owner_id: int, parts: tuple[str, ...],
                 attachments: tuple[AgentAttachment, ...] = (),
                 panel: Callable[[discord.Interaction], Awaitable[None]] | None = None,
                 panels: tuple[Callable[[discord.Interaction], Awaitable[None]], ...] = (),
                 panel_labels: tuple[str, ...] = ()):
        super().__init__(timeout=PRIVATE_RESULT_TIMEOUT)
        self.owner_id = owner_id
        parts = (*parts, *spreadsheet_warnings(attachments, parts))
        self.parts = tuple(chunk for part in parts for chunk in chunk_response(part))
        self.attachments = attachments
        self.panels = panels or ((panel,) if panel is not None else ())
        self.panel_labels = panel_labels
        self.message = None
        self.expired = False
        button = discord.ui.Button(label=ACTION_PRIVATE_BUTTON,
                                   style=discord.ButtonStyle.secondary)
        button.callback = self.open_result
        self.add_item(button)

    async def open_result(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(ACTION_RESULT_OWNER, ephemeral=True)
            return
        if self.expired:
            await interaction.response.send_message(ACTION_RESULT_EXPIRED, ephemeral=True)
            return
        files = attachment_files(self.attachments)
        links = spreadsheet_links(None, self.attachments)
        options = {"view": links} if links is not None else {}
        try:
            if self.panels:
                if len(self.panels) == 1:
                    await self.panels[0](interaction)
                else:
                    await interaction.response.send_message(
                        view=PrivatePanelSelect(self.owner_id, self.panels,
                                                self.panel_labels),
                        ephemeral=True,
                    )
                if files or self.parts or links is not None:
                    await interaction.followup.send(
                        self.parts[0] if self.parts else None,
                        files=files, ephemeral=True, **options,
                    )
                    for part in self.parts[1:]:
                        await interaction.followup.send(part, ephemeral=True)
                return
            for index, part in enumerate(self.parts or ("",)):
                if index == 0:
                    if files:
                        options["files"] = files
                    if options:
                        await interaction.response.send_message(
                            part or None, ephemeral=True, **options,
                        )
                    else:
                        await interaction.response.send_message(part, ephemeral=True)
                else:
                    await interaction.followup.send(part, ephemeral=True)
        finally:
            for file in files:
                file.close()

    async def on_timeout(self) -> None:
        self.expired = True
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.DiscordException:
                LOGGER.warning("Private command result could not be disabled")

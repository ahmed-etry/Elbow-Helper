"""Private delivery for command results."""

from __future__ import annotations

import logging
import io

import discord

from ..wording import (
    COMMAND_PRIVATE_BUTTON, COMMAND_RESULT_EXPIRED,
    COMMAND_RESULT_OWNER,
)
from ..models import AgentAttachment


LOGGER = logging.getLogger(__name__)
PRIVATE_RESULT_TIMEOUT = 600.0


class PrivateCommandView(discord.ui.View):
    def __init__(self, owner_id: int, parts: tuple[str, ...],
                 attachments: tuple[AgentAttachment, ...] = ()):
        super().__init__(timeout=PRIVATE_RESULT_TIMEOUT)
        self.owner_id = owner_id
        self.parts = parts
        self.attachments = attachments
        self.message = None
        self.expired = False
        button = discord.ui.Button(label=COMMAND_PRIVATE_BUTTON,
                                   style=discord.ButtonStyle.secondary)
        button.callback = self.open_result
        self.add_item(button)

    async def open_result(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(COMMAND_RESULT_OWNER, ephemeral=True)
            return
        if self.expired:
            await interaction.response.send_message(COMMAND_RESULT_EXPIRED, ephemeral=True)
            return
        files = [discord.File(io.BytesIO(item.data), filename=item.filename)
                 for item in self.attachments]
        try:
            for index, part in enumerate(self.parts or ("",)):
                if index == 0:
                    if files:
                        await interaction.response.send_message(
                            part or None, files=files, ephemeral=True,
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

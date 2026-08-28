from __future__ import annotations

import discord
from elbow_helper.discord.interactions import fail
from elbow_helper.discord.interactions import is_unknown_interaction_error
from elbow_helper.discord.interactions import log_interaction_error


class BaseTimeoutView(discord.ui.View):
    """View that disables controls and updates the bound message on timeout."""

    def __init__(self, *, timeout: float | None):
        super().__init__(timeout=timeout)
        self.message: discord.Message | None = None

    def bind_message(self, message: discord.Message | None) -> "BaseTimeoutView":
        self.message = message
        return self

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[discord.ui.View],
    ) -> None:
        source = getattr(item, "custom_id", None) or item.__class__.__name__
        if is_unknown_interaction_error(error):
            log_interaction_error(interaction, error, source=f"{self.__class__.__name__}.{source}")
            return
        log_interaction_error(interaction, error, source=f"{self.__class__.__name__}.{source}")
        await fail(interaction)

    async def on_timeout(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass


class BaseErrorModal(discord.ui.Modal):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        if is_unknown_interaction_error(error):
            log_interaction_error(interaction, error, source=self.__class__.__name__)
            return
        log_interaction_error(interaction, error, source=self.__class__.__name__)
        await fail(interaction)


def _trim_button_label(label: str, limit: int = 80) -> str:
    if len(label) <= limit:
        return label
    return label[: limit - 3] + "..."


class TranscriptLinkPromptButton(discord.ui.Button):
    def __init__(self, custom_id: str):
        super().__init__(
            label="Direct Link",
            emoji="📎",
            style=discord.ButtonStyle.primary,
            custom_id=custom_id,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        source_message = interaction.message
        attachment = source_message.attachments[0] if source_message and source_message.attachments else None
        if attachment is None:
            await interaction.response.send_message("The transcript file is no longer available.", ephemeral=True)
            return

        await interaction.response.edit_message(
            view=TranscriptLinkPromptView(
                self.custom_id or "transcript_link",
                attachment_name=attachment.filename,
                attachment_url=attachment.url,
            )
        )


class TranscriptLinkPromptView(BaseTimeoutView):
    def __init__(
        self,
        custom_id: str,
        *,
        attachment_name: str | None = None,
        attachment_url: str | None = None,
    ):
        super().__init__(timeout=None)
        self.add_item(TranscriptLinkPromptButton(custom_id))
        if attachment_name and attachment_url:
            self.add_item(
                discord.ui.Button(
                    label=_trim_button_label(attachment_name),
                    style=discord.ButtonStyle.link,
                    url=attachment_url,
                )
            )

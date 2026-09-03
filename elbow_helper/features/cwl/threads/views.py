"""Persistent controls for CWL thread status boards."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from elbow_helper.discord.views import BaseTimeoutView

from .emojis import CwlThreadEmojiSet
from .emojis import EMPTY_CWL_THREAD_EMOJIS


if TYPE_CHECKING:
    from ..cog import CwlManagement


CC_STATUS_PRESENTATION = {
    "filled": ("Filled", discord.ButtonStyle.success, "filled", "✅"),
    "partial": ("Partial", discord.ButtonStyle.secondary, None, "⚠️"),
    "empty": ("Empty", discord.ButtonStyle.danger, "empty", "❌"),
}


class CwlCcStatusButton(discord.ui.Button):
    def __init__(
        self,
        cog: CwlManagement,
        clan_code: str,
        status: str,
        *,
        current_status: str | None,
        emojis: CwlThreadEmojiSet,
    ) -> None:
        label, style, emoji_key, fallback = CC_STATUS_PRESENTATION[status]
        super().__init__(
            label=label,
            emoji=emojis.icon(emoji_key, fallback) if emoji_key else fallback,
            style=style,
            custom_id=f"cwl:cc_status:{clan_code.casefold()}:{status}",
            disabled=status == current_status,
        )
        self.cog = cog
        self.clan_code = clan_code
        self.status = status

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.update_cc_status_from_button(
            interaction,
            self.clan_code,
            self.status,
        )


class CwlCcStatusView(BaseTimeoutView):
    def __init__(
        self,
        cog: CwlManagement,
        clan_code: str,
        *,
        current_status: str | None = None,
        emojis: CwlThreadEmojiSet = EMPTY_CWL_THREAD_EMOJIS,
    ) -> None:
        super().__init__(timeout=None)
        for status in CC_STATUS_PRESENTATION:
            self.add_item(
                CwlCcStatusButton(
                    cog,
                    clan_code,
                    status,
                    current_status=current_status,
                    emojis=emojis,
                )
            )

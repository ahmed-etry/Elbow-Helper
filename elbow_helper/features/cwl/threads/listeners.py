"""Thread feature listeners."""

from __future__ import annotations

import discord
from discord.ext import commands


class CwlThreadListenerMixin:
    @commands.Cog.listener()
    async def on_message(self, message):
        """Reposition a buried CWL board only in response to human activity."""
        if not isinstance(message.channel, discord.Thread):
            return
        if getattr(message.author, "bot", False):
            return

        thread_id = str(message.channel.id)
        thread_data = self.data["threads"].get(thread_id)
        if not isinstance(thread_data, dict):
            return

        # Ignore the sticky embed itself so refresh/repost actions do not self-trigger.
        if message.id == thread_data.get("sticky_message_id"):
            return
        await self._repost_thread_status_from_activity(message.channel)

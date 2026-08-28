"""Thread feature listeners."""

from __future__ import annotations

import discord
from discord.ext import commands


class CwlThreadListenerMixin:
    @commands.Cog.listener()
    async def on_message(self, message):
        """Track message activity in CWL threads."""
        if not isinstance(message.channel, discord.Thread):
            return

        thread_id = str(message.channel.id)
        thread_data = self.data["threads"].get(thread_id)
        if not isinstance(thread_data, dict):
            return

        # Ignore the sticky embed itself so refresh/repost actions do not self-trigger.
        if message.id == thread_data.get("sticky_message_id"):
            return

        self.last_message_times[thread_id] = self._utc_now()
        self.conversation_active[thread_id] = True
        self.sticky_repositioned[thread_id] = False

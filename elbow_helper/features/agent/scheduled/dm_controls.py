"""Persistent recipient-owned controls for repeating reminder DMs."""

import asyncio

import discord

from ..wording import ACTION_REMINDER_DM_STOP, ACTION_REMINDER_DM_STOPPED, ACTION_UNAVAILABLE


class ReminderStopButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"agent:reminder-stop:(?P<rule>[a-f0-9]{32}):(?P<member>[0-9]+)",
):
    def __init__(self, identifier, member_id, repository=None):
        self.identifier = identifier
        self.member_id = member_id
        self.repository = repository
        super().__init__(discord.ui.Button(
            label=ACTION_REMINDER_DM_STOP, style=discord.ButtonStyle.secondary,
            custom_id=f"agent:reminder-stop:{identifier}:{member_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["rule"], int(match["member"]))

    async def callback(self, interaction):
        if interaction.user.id != self.member_id:
            await interaction.response.defer(ephemeral=True)
            return
        cog = interaction.client.get_cog("AgentCog") if self.repository is None else None
        repository = self.repository or getattr(cog, "action_repository", None)
        if repository is None:
            await interaction.response.send_message(ACTION_UNAVAILABLE, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await asyncio.to_thread(
            repository.stop_reminder_recipient,
            identifier=self.identifier, member_id=self.member_id,
        )
        await interaction.followup.send(ACTION_REMINDER_DM_STOPPED, ephemeral=True)


def reminder_stop_view(repository, identifier, member_id):
    view = discord.ui.View(timeout=None)
    view.add_item(ReminderStopButton(identifier, member_id, repository))
    return view

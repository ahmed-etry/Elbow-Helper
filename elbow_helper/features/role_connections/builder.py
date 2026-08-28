"""Interactive views for creating role connection rules."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from elbow_helper.discord.interactions import edit_bound_view
from elbow_helper.discord.views import BaseTimeoutView

from .formatting import _conditions_to_lines
from .formatting import _format_condition
from .formatting import _list_label
from .formatting import _role_mention
from .state import save_state
from .view_utils import build_embed

if TYPE_CHECKING:
    from .cog import RoleConnections


class TargetRoleSelect(discord.ui.RoleSelect):
    def __init__(self, view: "TargetRoleSelectView"):
        super().__init__(placeholder="Choose role to manage", min_values=1, max_values=1)
        self._parent_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        self._parent_view.target_role_id = self.values[0].id
        builder = ConditionBuilderView(self._parent_view.cog, self._parent_view.channel, self._parent_view.target_role_id)
        self._parent_view.stop()
        await edit_bound_view(interaction, embed=builder.build_embed(), view=builder)


class TargetRoleSelectView(BaseTimeoutView):
    def __init__(self, cog: "RoleConnections", channel: discord.TextChannel):
        super().__init__(timeout=300)
        self.cog = cog
        self.channel = channel
        self.target_role_id: int | None = None
        self.add_item(TargetRoleSelect(self))
        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

    def build_embed(self) -> discord.Embed:
        return build_embed("Add Role Connection", "Choose the role this connection will manage.")

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="No changes made.", embed=None, view=None)


class ConditionTypeSelect(discord.ui.Select):
    def __init__(self, view: "ConditionBuilderView"):
        options = [
            discord.SelectOption(label=_list_label("all", "has"), value="all:has"),
            discord.SelectOption(label=_list_label("all", "not"), value="all:not"),
            discord.SelectOption(label=_list_label("any", "has"), value="any:has"),
            discord.SelectOption(label=_list_label("any", "not"), value="any:not"),
        ]
        super().__init__(placeholder="Select condition type", options=options, min_values=1, max_values=1)
        self._parent_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        list_name, kind = self.values[0].split(":")
        self._parent_view.selected_list = list_name
        self._parent_view.selected_kind = kind
        await interaction.response.edit_message(embed=self._parent_view.build_embed(), view=self._parent_view)


class ConditionRoleSelect(discord.ui.RoleSelect):
    def __init__(self, view: "ConditionBuilderView"):
        super().__init__(placeholder="Select role for condition", min_values=1, max_values=1)
        self._parent_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self._parent_view.selected_list or not self._parent_view.selected_kind:
            await interaction.response.send_message("Choose a condition type first.", ephemeral=True)
            return
        role_id = self.values[0].id
        self._parent_view.add_condition(self._parent_view.selected_list, self._parent_view.selected_kind, role_id)
        await interaction.response.edit_message(embed=self._parent_view.build_embed(), view=self._parent_view)


class ConditionBuilderView(BaseTimeoutView):
    def __init__(self, cog: "RoleConnections", channel: discord.TextChannel, target_role_id: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.channel = channel
        self.target_role_id = target_role_id
        self.conditions_all: list[dict[str, int]] = []
        self.conditions_any: list[dict[str, int]] = []
        self.selected_list: str | None = None
        self.selected_kind: str | None = None

        self.add_item(ConditionTypeSelect(self))
        self.add_item(ConditionRoleSelect(self))

        finish_button = discord.ui.Button(label="Finish", style=discord.ButtonStyle.success)
        finish_button.callback = self.finish
        self.add_item(finish_button)

        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

    def add_condition(self, list_name: str, kind: str, role_id: int) -> None:
        entry = {"has": role_id} if kind == "has" else {"not": role_id}
        target_list = self.conditions_all if list_name == "all" else self.conditions_any
        if entry not in target_list:
            target_list.append(entry)

    def build_embed(self) -> discord.Embed:
        embed = build_embed("Add Role Connection")
        embed.add_field(name="Role to manage", value=_role_mention(self.target_role_id), inline=False)
        if self.selected_list and self.selected_kind:
            embed.add_field(name="Next condition", value=_list_label(self.selected_list, self.selected_kind), inline=False)

        lines: list[str] = []
        for cond in self.conditions_all:
            kind, role = _format_condition(cond)
            lines.append(f"Member has {role}" if kind == "has" else f"Member doesn't have {role}")
        if self.conditions_any:
            lines.extend(_conditions_to_lines({"any": self.conditions_any}))
        if not lines:
            lines.append("No conditions added yet.")

        embed.add_field(name="Conditions", value="\n".join(lines), inline=False)
        return embed

    async def finish(self, interaction: discord.Interaction) -> None:
        if not self.conditions_all and not self.conditions_any:
            await interaction.response.send_message("Add at least one condition before finishing.", ephemeral=True)
            return
        connection = {
            "id": self.cog.new_connection_id(),
            "target_role_id": self.target_role_id,
            "all": self.conditions_all,
            "any": self.conditions_any,
        }
        self.cog.state["connections"].append(connection)
        save_state(self.cog.state)
        board_message = await self.cog.refresh_connections_message(self.channel)
        self.stop()
        await interaction.response.edit_message(
            content=f"Role connection added. Updated board: {board_message.jump_url}",
            embed=None,
            view=None,
        )

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="No changes made.", embed=None, view=None)


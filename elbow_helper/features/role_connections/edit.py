"""Interactive views for editing and removing connection rules."""

from __future__ import annotations

import copy
import math
from typing import TYPE_CHECKING

import discord
from elbow_helper.discord.interactions import edit_bound_view
from elbow_helper.discord.pagination import ADAPTIVE_JUMP_THRESHOLD
from elbow_helper.discord.pagination import FIRST_PAGE_LABEL
from elbow_helper.discord.pagination import format_page_footer
from elbow_helper.discord.pagination import LAST_PAGE_LABEL
from elbow_helper.discord.pagination import NEXT_PAGE_LABEL
from elbow_helper.discord.pagination import PREV_PAGE_LABEL
from elbow_helper.discord.views import BaseTimeoutView

from .config import INVALID_DEPENDENCY_MESSAGE
from .config import PERSISTENCE_ERROR_MESSAGE
from .config import SELECTOR_PAGE_SIZE
from .formatting import _conditions_summary
from .formatting import _conditions_to_lines
from .formatting import _list_label
from .formatting import _role_mention
from .view_utils import build_embed
from .view_utils import role_name

if TYPE_CHECKING:
    from .cog import RoleConnections


def _connection_page(
    cog: "RoleConnections",
    channel: discord.TextChannel,
    page: int,
) -> tuple[int, int, list[discord.SelectOption]]:
    total_connections = len(cog.state["connections"])
    total_pages = max(1, math.ceil(total_connections / SELECTOR_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * SELECTOR_PAGE_SIZE
    end = start + SELECTOR_PAGE_SIZE

    options: list[discord.SelectOption] = []
    guild = channel.guild
    for connection in cog.state["connections"][start:end]:
        summary = _conditions_summary(connection)
        options.append(
            discord.SelectOption(
                label=f"Manage {role_name(guild, connection['target_role_id'])}",
                value=connection["id"],
                description=summary[:100],
            )
        )
    return page, total_pages, options


class RemoveConnectionSelect(discord.ui.Select):
    def __init__(self, view: "RemoveConnectionView", options: list[discord.SelectOption]):
        super().__init__(placeholder="Select connection to remove", options=options, min_values=1, max_values=1)
        self._parent_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        conn_id = self.values[0]
        try:
            removed = self._parent_view.cog.remove_connection(conn_id)
        except (OSError, TypeError):
            await interaction.response.send_message(
                PERSISTENCE_ERROR_MESSAGE,
                ephemeral=True,
            )
            return
        if removed:
            board_message = await self._parent_view.cog.refresh_connections_message(self._parent_view.channel)
            if self._parent_view.page > 0 and self._parent_view.page >= self._parent_view.cog.get_selector_page_count():
                self._parent_view.page -= 1
            self._parent_view.stop()
            await interaction.response.edit_message(
                content=f"Role connection removed. Updated board: {board_message.jump_url}",
                embed=None,
                view=None,
            )
        else:
            await interaction.response.send_message("That role connection is no longer available.", ephemeral=True)


class RemoveConnectionView(BaseTimeoutView):
    def __init__(self, cog: "RoleConnections", channel: discord.TextChannel, page: int = 0):
        super().__init__(timeout=300)
        self.cog = cog
        self.channel = channel
        self.page, self.total_pages, options = _connection_page(cog, channel, page)

        if options:
            self.add_item(RemoveConnectionSelect(self, options))

        if self.total_pages > ADAPTIVE_JUMP_THRESHOLD:
            self.first_button = discord.ui.Button(label=FIRST_PAGE_LABEL, style=discord.ButtonStyle.secondary)
            self.first_button.callback = self.first_page
            self.first_button.disabled = self.page <= 0
            self.add_item(self.first_button)
        else:
            self.first_button = None

        self.prev_button = discord.ui.Button(label=PREV_PAGE_LABEL, style=discord.ButtonStyle.secondary)
        self.prev_button.callback = self.prev_page
        self.prev_button.disabled = self.page <= 0
        self.add_item(self.prev_button)

        self.next_button = discord.ui.Button(label=NEXT_PAGE_LABEL, style=discord.ButtonStyle.secondary)
        self.next_button.callback = self.next_page
        self.next_button.disabled = self.page >= self.total_pages - 1
        self.add_item(self.next_button)

        if self.total_pages > ADAPTIVE_JUMP_THRESHOLD:
            self.last_button = discord.ui.Button(label=LAST_PAGE_LABEL, style=discord.ButtonStyle.secondary)
            self.last_button.callback = self.last_page
            self.last_button.disabled = self.page >= self.total_pages - 1
            self.add_item(self.last_button)
        else:
            self.last_button = None

        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

    def build_embed(self) -> discord.Embed:
        embed = build_embed("Remove Connection", "Choose a role connection to remove.")
        if self.total_pages > 1:
            embed.set_footer(text=format_page_footer(self.page + 1, self.total_pages))
        return embed

    async def prev_page(self, interaction: discord.Interaction) -> None:
        view = RemoveConnectionView(self.cog, self.channel, page=self.page - 1)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def first_page(self, interaction: discord.Interaction) -> None:
        view = RemoveConnectionView(self.cog, self.channel, page=0)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def next_page(self, interaction: discord.Interaction) -> None:
        view = RemoveConnectionView(self.cog, self.channel, page=self.page + 1)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def last_page(self, interaction: discord.Interaction) -> None:
        view = RemoveConnectionView(self.cog, self.channel, page=self.total_pages - 1)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="No changes made.", embed=None, view=None)


class EditConnectionSelect(discord.ui.Select):
    def __init__(self, view: "EditConnectionView", options: list[discord.SelectOption]):
        super().__init__(placeholder="Select connection to edit", options=options, min_values=1, max_values=1)
        self._parent_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        conn_id = self.values[0]
        view = EditConnectionActionView(self._parent_view.cog, self._parent_view.channel, conn_id)
        self._parent_view.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)


class EditConnectionView(BaseTimeoutView):
    def __init__(self, cog: "RoleConnections", channel: discord.TextChannel, page: int = 0):
        super().__init__(timeout=300)
        self.cog = cog
        self.channel = channel
        self.page, self.total_pages, options = _connection_page(cog, channel, page)

        if options:
            self.add_item(EditConnectionSelect(self, options))

        if self.total_pages > ADAPTIVE_JUMP_THRESHOLD:
            self.first_button = discord.ui.Button(label=FIRST_PAGE_LABEL, style=discord.ButtonStyle.secondary)
            self.first_button.callback = self.first_page
            self.first_button.disabled = self.page <= 0
            self.add_item(self.first_button)
        else:
            self.first_button = None

        self.prev_button = discord.ui.Button(label=PREV_PAGE_LABEL, style=discord.ButtonStyle.secondary)
        self.prev_button.callback = self.prev_page
        self.prev_button.disabled = self.page <= 0
        self.add_item(self.prev_button)

        self.next_button = discord.ui.Button(label=NEXT_PAGE_LABEL, style=discord.ButtonStyle.secondary)
        self.next_button.callback = self.next_page
        self.next_button.disabled = self.page >= self.total_pages - 1
        self.add_item(self.next_button)

        if self.total_pages > ADAPTIVE_JUMP_THRESHOLD:
            self.last_button = discord.ui.Button(label=LAST_PAGE_LABEL, style=discord.ButtonStyle.secondary)
            self.last_button.callback = self.last_page
            self.last_button.disabled = self.page >= self.total_pages - 1
            self.add_item(self.last_button)
        else:
            self.last_button = None

        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

    def build_embed(self) -> discord.Embed:
        embed = build_embed("Edit Connection", "Choose a role connection to edit.")
        if self.total_pages > 1:
            embed.set_footer(text=format_page_footer(self.page + 1, self.total_pages))
        return embed

    async def prev_page(self, interaction: discord.Interaction) -> None:
        view = EditConnectionView(self.cog, self.channel, page=self.page - 1)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def first_page(self, interaction: discord.Interaction) -> None:
        view = EditConnectionView(self.cog, self.channel, page=0)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def next_page(self, interaction: discord.Interaction) -> None:
        view = EditConnectionView(self.cog, self.channel, page=self.page + 1)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def last_page(self, interaction: discord.Interaction) -> None:
        view = EditConnectionView(self.cog, self.channel, page=self.total_pages - 1)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="No changes made.", embed=None, view=None)


class EditConnectionActionView(BaseTimeoutView):
    def __init__(self, cog: "RoleConnections", channel: discord.TextChannel, conn_id: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.channel = channel
        self.conn_id = conn_id

        target_button = discord.ui.Button(label="Role to manage", style=discord.ButtonStyle.primary)
        target_button.callback = self.edit_target_role
        self.add_item(target_button)

        all_has_button = discord.ui.Button(label=_list_label("all", "has"), style=discord.ButtonStyle.secondary)
        all_has_button.callback = self.edit_all_has
        self.add_item(all_has_button)

        all_not_button = discord.ui.Button(label=_list_label("all", "not"), style=discord.ButtonStyle.secondary)
        all_not_button.callback = self.edit_all_not
        self.add_item(all_not_button)

        any_has_button = discord.ui.Button(label=_list_label("any", "has"), style=discord.ButtonStyle.secondary)
        any_has_button.callback = self.edit_any_has
        self.add_item(any_has_button)

        any_not_button = discord.ui.Button(label=_list_label("any", "not"), style=discord.ButtonStyle.secondary)
        any_not_button.callback = self.edit_any_not
        self.add_item(any_not_button)

        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

    def build_embed(self) -> discord.Embed:
        connection = self.cog.get_connection(self.conn_id)
        embed = build_embed("Edit Connection")
        if not connection:
            embed.description = "That role connection is no longer available."
            return embed
        embed.add_field(name="Role to manage", value=_role_mention(connection["target_role_id"]), inline=False)
        lines = _conditions_to_lines(connection)
        embed.add_field(name="Conditions", value="\n".join(lines) if lines else "No conditions", inline=False)
        return embed

    async def _start_role_list_edit(self, interaction: discord.Interaction, list_name: str, kind: str) -> None:
        view = RoleListEditActionView(self.cog, self.channel, self.conn_id, list_name, kind)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def edit_target_role(self, interaction: discord.Interaction) -> None:
        view = TargetRoleEditView(self.cog, self.channel, self.conn_id)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def edit_all_has(self, interaction: discord.Interaction) -> None:
        await self._start_role_list_edit(interaction, "all", "has")

    async def edit_all_not(self, interaction: discord.Interaction) -> None:
        await self._start_role_list_edit(interaction, "all", "not")

    async def edit_any_has(self, interaction: discord.Interaction) -> None:
        await self._start_role_list_edit(interaction, "any", "has")

    async def edit_any_not(self, interaction: discord.Interaction) -> None:
        await self._start_role_list_edit(interaction, "any", "not")

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="No changes made.", embed=None, view=None)


class TargetRoleEditSelect(discord.ui.RoleSelect):
    def __init__(self, view: "TargetRoleEditView"):
        super().__init__(placeholder="Choose new role to manage", min_values=1, max_values=1)
        self._parent_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        role_id = self.values[0].id
        connection = self._parent_view.cog.get_connection(self._parent_view.conn_id)
        if not connection:
            await interaction.response.send_message("That role connection is no longer available.", ephemeral=True)
            return
        candidate = copy.deepcopy(connection)
        candidate["target_role_id"] = role_id
        if not self._parent_view.cog.connection_change_is_valid(
            candidate,
            replacing_id=self._parent_view.conn_id,
        ):
            await interaction.response.send_message(
                INVALID_DEPENDENCY_MESSAGE,
                ephemeral=True,
            )
            return
        try:
            updated = self._parent_view.cog.update_connection_target(
                self._parent_view.conn_id,
                role_id,
            )
        except (OSError, TypeError):
            await interaction.response.send_message(
                PERSISTENCE_ERROR_MESSAGE,
                ephemeral=True,
            )
            return
        if not updated:
            await interaction.response.send_message("That role connection is no longer available.", ephemeral=True)
            return
        board_message = await self._parent_view.cog.refresh_connections_message(self._parent_view.channel)
        self._parent_view.stop()
        await interaction.response.edit_message(
            content=f"Role connection updated. View the board: {board_message.jump_url}",
            embed=None,
            view=None,
        )


class TargetRoleEditView(BaseTimeoutView):
    def __init__(self, cog: "RoleConnections", channel: discord.TextChannel, conn_id: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.channel = channel
        self.conn_id = conn_id
        self.add_item(TargetRoleEditSelect(self))
        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

    def build_embed(self) -> discord.Embed:
        connection = self.cog.get_connection(self.conn_id)
        embed = build_embed("Change Managed Role", "Choose the new role this connection will manage.")
        if connection:
            embed.add_field(name="Current role", value=_role_mention(connection["target_role_id"]), inline=False)
        return embed

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="No changes made.", embed=None, view=None)


class RoleListAddSelect(discord.ui.RoleSelect):
    def __init__(self, view: "RoleListAddView"):
        super().__init__(placeholder="Select roles to add", min_values=1, max_values=25)
        self._parent_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        role_ids = [role.id for role in self.values]
        connection = self._parent_view.cog.get_connection(self._parent_view.conn_id)
        if not connection:
            await interaction.response.send_message("That role connection is no longer available.", ephemeral=True)
            return
        candidate = copy.deepcopy(connection)
        key = "has" if self._parent_view.kind == "has" else "not"
        target = candidate.setdefault(self._parent_view.list_name, [])
        for role_id in role_ids:
            condition = {key: role_id}
            if condition not in target:
                target.append(condition)
        if not self._parent_view.cog.connection_change_is_valid(
            candidate,
            replacing_id=self._parent_view.conn_id,
        ):
            await interaction.response.send_message(
                INVALID_DEPENDENCY_MESSAGE,
                ephemeral=True,
            )
            return
        try:
            updated = self._parent_view.cog.add_connection_roles(
                self._parent_view.conn_id,
                self._parent_view.list_name,
                self._parent_view.kind,
                role_ids,
            )
        except (OSError, TypeError):
            await interaction.response.send_message(
                PERSISTENCE_ERROR_MESSAGE,
                ephemeral=True,
            )
            return
        if not updated:
            await interaction.response.send_message("That role connection is no longer available.", ephemeral=True)
            return
        board_message = await self._parent_view.cog.refresh_connections_message(self._parent_view.channel)
        self._parent_view.stop()
        await interaction.response.edit_message(
            content=f"Role connection updated. View the board: {board_message.jump_url}",
            embed=None,
            view=None,
        )


class RoleListRemoveSelect(discord.ui.Select):
    def __init__(self, view: "RoleListRemoveView", options: list[discord.SelectOption]):
        super().__init__(
            placeholder="Select roles to remove",
            options=options,
            min_values=1,
            max_values=min(25, len(options)),
        )
        self._parent_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        role_ids = [int(value) for value in self.values]
        try:
            updated = self._parent_view.cog.remove_connection_roles(
                self._parent_view.conn_id,
                self._parent_view.list_name,
                self._parent_view.kind,
                role_ids,
            )
        except (OSError, TypeError):
            await interaction.response.send_message(
                PERSISTENCE_ERROR_MESSAGE,
                ephemeral=True,
            )
            return
        if not updated:
            await interaction.response.send_message("That role connection is no longer available.", ephemeral=True)
            return
        board_message = await self._parent_view.cog.refresh_connections_message(self._parent_view.channel)
        refreshed = RoleListRemoveView(
            self._parent_view.cog,
            self._parent_view.channel,
            self._parent_view.conn_id,
            self._parent_view.list_name,
            self._parent_view.kind,
            page=self._parent_view.page,
        )
        self._parent_view.stop()
        if not refreshed.options:
            await interaction.response.edit_message(
                content=f"Role connection updated. View the board: {board_message.jump_url}",
                embed=None,
                view=None,
            )
            return
        await edit_bound_view(
            interaction,
            content=f"Role connection updated. View the board: {board_message.jump_url}",
            embed=refreshed.build_embed(),
            view=refreshed,
        )


class RoleListEditActionView(BaseTimeoutView):
    def __init__(
        self,
        cog: "RoleConnections",
        channel: discord.TextChannel,
        conn_id: str,
        list_name: str,
        kind: str,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.channel = channel
        self.conn_id = conn_id
        self.list_name = list_name
        self.kind = kind

        add_button = discord.ui.Button(label="Add role", style=discord.ButtonStyle.success)
        add_button.callback = self.add_role
        self.add_item(add_button)

        remove_button = discord.ui.Button(label="Remove role", style=discord.ButtonStyle.danger)
        remove_button.callback = self.remove_role
        self.add_item(remove_button)

        back_button = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back_button.callback = self.back
        self.add_item(back_button)

    def build_embed(self) -> discord.Embed:
        connection = self.cog.get_connection(self.conn_id)
        list_label = _list_label(self.list_name, self.kind)
        embed = build_embed("Edit Connection", f"Update roles under **{list_label}**.")
        if connection:
            current_ids = self.cog.get_connection_list_ids(connection, self.list_name, self.kind)
            current_text = ", ".join(_role_mention(role_id) for role_id in current_ids) if current_ids else "None"
            embed.add_field(name="Current roles", value=current_text, inline=False)
        return embed

    async def add_role(self, interaction: discord.Interaction) -> None:
        view = RoleListAddView(self.cog, self.channel, self.conn_id, self.list_name, self.kind)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def remove_role(self, interaction: discord.Interaction) -> None:
        view = RoleListRemoveView(self.cog, self.channel, self.conn_id, self.list_name, self.kind, page=0)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def back(self, interaction: discord.Interaction) -> None:
        view = EditConnectionActionView(self.cog, self.channel, self.conn_id)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)


class RoleListAddView(BaseTimeoutView):
    def __init__(
        self,
        cog: "RoleConnections",
        channel: discord.TextChannel,
        conn_id: str,
        list_name: str,
        kind: str,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.channel = channel
        self.conn_id = conn_id
        self.list_name = list_name
        self.kind = kind
        self.add_item(RoleListAddSelect(self))
        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

    def build_embed(self) -> discord.Embed:
        return build_embed("Add Roles", f"Choose roles to add under **{_list_label(self.list_name, self.kind)}**.")

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="No changes made.", embed=None, view=None)


class RoleListRemoveView(BaseTimeoutView):
    def __init__(
        self,
        cog: "RoleConnections",
        channel: discord.TextChannel,
        conn_id: str,
        list_name: str,
        kind: str,
        page: int = 0,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.channel = channel
        self.conn_id = conn_id
        self.list_name = list_name
        self.kind = kind

        connection = self.cog.get_connection(self.conn_id)
        ids = self.cog.get_connection_list_ids(connection, self.list_name, self.kind) if connection else []
        self.total_pages = max(1, math.ceil(len(ids) / SELECTOR_PAGE_SIZE))
        self.page = max(0, min(page, self.total_pages - 1))
        start = self.page * SELECTOR_PAGE_SIZE
        end = start + SELECTOR_PAGE_SIZE
        self.options = [
            discord.SelectOption(label=role_name(channel.guild, role_id), value=str(role_id))
            for role_id in ids[start:end]
        ]

        if self.options:
            self.add_item(RoleListRemoveSelect(self, self.options))

        if self.total_pages > ADAPTIVE_JUMP_THRESHOLD:
            self.first_button = discord.ui.Button(label=FIRST_PAGE_LABEL, style=discord.ButtonStyle.secondary)
            self.first_button.callback = self.first_page
            self.first_button.disabled = self.page <= 0
            self.add_item(self.first_button)
        else:
            self.first_button = None

        self.prev_button = discord.ui.Button(label=PREV_PAGE_LABEL, style=discord.ButtonStyle.secondary)
        self.prev_button.callback = self.prev_page
        self.prev_button.disabled = self.page <= 0
        self.add_item(self.prev_button)

        self.next_button = discord.ui.Button(label=NEXT_PAGE_LABEL, style=discord.ButtonStyle.secondary)
        self.next_button.callback = self.next_page
        self.next_button.disabled = self.page >= self.total_pages - 1
        self.add_item(self.next_button)

        if self.total_pages > ADAPTIVE_JUMP_THRESHOLD:
            self.last_button = discord.ui.Button(label=LAST_PAGE_LABEL, style=discord.ButtonStyle.secondary)
            self.last_button.callback = self.last_page
            self.last_button.disabled = self.page >= self.total_pages - 1
            self.add_item(self.last_button)
        else:
            self.last_button = None

        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

    def build_embed(self) -> discord.Embed:
        description = f"Choose roles to remove from **{_list_label(self.list_name, self.kind)}**."
        if not self.options:
            description += "\n\nNo roles are set in this list yet."
        embed = build_embed("Remove Roles", description)
        if self.total_pages > 1:
            embed.set_footer(text=format_page_footer(self.page + 1, self.total_pages))
        return embed

    async def prev_page(self, interaction: discord.Interaction) -> None:
        view = RoleListRemoveView(self.cog, self.channel, self.conn_id, self.list_name, self.kind, page=self.page - 1)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def first_page(self, interaction: discord.Interaction) -> None:
        view = RoleListRemoveView(self.cog, self.channel, self.conn_id, self.list_name, self.kind, page=0)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def next_page(self, interaction: discord.Interaction) -> None:
        view = RoleListRemoveView(self.cog, self.channel, self.conn_id, self.list_name, self.kind, page=self.page + 1)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def last_page(self, interaction: discord.Interaction) -> None:
        view = RoleListRemoveView(
            self.cog,
            self.channel,
            self.conn_id,
            self.list_name,
            self.kind,
            page=self.total_pages - 1,
        )
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="No changes made.", embed=None, view=None)


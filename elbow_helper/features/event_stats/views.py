"""Interactive event manager views."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from elbow_helper.discord.interactions import edit_bound_view, edit_original_bound_view
from elbow_helper.discord.pagination import ADAPTIVE_JUMP_THRESHOLD
from elbow_helper.discord.pagination import FIRST_PAGE_LABEL
from elbow_helper.discord.pagination import format_page_footer
from elbow_helper.discord.pagination import LAST_PAGE_LABEL
from elbow_helper.discord.pagination import NEXT_PAGE_LABEL
from elbow_helper.discord.pagination import PREV_PAGE_LABEL
from elbow_helper.discord.views import BaseErrorModal
from elbow_helper.discord.views import BaseTimeoutView

from elbow_helper.domain.timezones import canonical_timezone_name
from elbow_helper.domain.timezones import resolve_timezone_input

from .config import DEFAULT_GRACE_HOURS
from .config import EVENT_LIST_PAGE_SIZE
from .config import EVENT_SELECTOR_PAGE_SIZE
from .config import MAX_EVENT_NAME_LENGTH
from .config import MAX_GRACE_HOURS
from .timeutils import format_event_datetime_local
from .timeutils import parse_event_datetime_input

if TYPE_CHECKING:
    from .cog import EventStatsCog


async def _ensure_manage_permission(cog: "EventStatsCog", interaction: discord.Interaction) -> bool:
    if cog._can_manage(interaction.user):
        return True
    await interaction.response.send_message("You don't have permission to manage events.", ephemeral=True)
    return False


def _parse_grace_hours(raw_value: str) -> int | None:
    value = (raw_value or "").strip()
    if not value:
        return DEFAULT_GRACE_HOURS
    try:
        hours = int(value)
    except ValueError:
        return None
    return max(0, min(hours, MAX_GRACE_HOURS))


class EventPanelView(BaseTimeoutView):
    def __init__(self, cog: "EventStatsCog", guild: discord.Guild | None):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild = guild

    @discord.ui.button(label="Create", style=discord.ButtonStyle.success)
    async def create_event(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _ensure_manage_permission(self.cog, interaction):
            return
        await interaction.response.send_modal(OneTimeEventModal(self.cog, self.guild))

    @discord.ui.button(label="Manage", style=discord.ButtonStyle.primary)
    async def manage_events(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _ensure_manage_permission(self.cog, interaction):
            return
        view = EventManageSelectView(self.cog, self.guild, page=0)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    @discord.ui.button(label="View All", style=discord.ButtonStyle.secondary)
    async def list_events(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _ensure_manage_permission(self.cog, interaction):
            return
        view = EventListView(self.cog, self.guild, page=0)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    @discord.ui.button(label="Update", style=discord.ButtonStyle.secondary)
    async def refresh_events(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _ensure_manage_permission(self.cog, interaction):
            return
        if self.guild is None:
            await interaction.response.send_message(
                "I couldn't reach the server right now. Try again in a moment.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        await self.cog.force_refresh(self.guild)
        self.stop()
        await edit_original_bound_view(
            interaction,
            embed=self.cog.build_panel_embed(self.guild),
            view=EventPanelView(self.cog, self.guild),
        )

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
    async def close_panel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Event panel closed.", embed=None, view=None)


class EventListView(BaseTimeoutView):
    def __init__(self, cog: "EventStatsCog", guild: discord.Guild | None, page: int = 0):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild = guild
        self.total_pages = max(1, (len(self.cog.events) + EVENT_LIST_PAGE_SIZE - 1) // EVENT_LIST_PAGE_SIZE)
        self.page = max(0, min(page, self.total_pages - 1))
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        return self.cog.build_event_list_embed(self.guild, self.page)

    @discord.ui.button(label=PREV_PAGE_LABEL, style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = EventListView(self.cog, self.guild, page=max(0, self.page - 1))
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    @discord.ui.button(label=NEXT_PAGE_LABEL, style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = EventListView(self.cog, self.guild, page=min(self.total_pages - 1, self.page + 1))
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    @discord.ui.button(label="Manage", style=discord.ButtonStyle.primary)
    async def manage_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = EventManageSelectView(self.cog, self.guild, page=0)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    @discord.ui.button(label="Overview", style=discord.ButtonStyle.secondary)
    async def panel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await edit_bound_view(
            interaction,
            embed=self.cog.build_panel_embed(self.guild),
            view=EventPanelView(self.cog, self.guild),
        )

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Event panel closed.", embed=None, view=None)


class EventManageSelect(discord.ui.Select):
    def __init__(self, view: "EventManageSelectView", options: list[discord.SelectOption]):
        super().__init__(placeholder="Choose an event", min_values=1, max_values=1, options=options)
        self._parent_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        key = self.values[0]
        view = EventActionView(self._parent_view.cog, self._parent_view.guild, key, return_page=self._parent_view.page)
        self._parent_view.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)


class EventManageSelectView(BaseTimeoutView):
    def __init__(self, cog: "EventStatsCog", guild: discord.Guild | None, page: int = 0):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild = guild
        self.total_pages = max(1, (len(self.cog.events) + EVENT_SELECTOR_PAGE_SIZE - 1) // EVENT_SELECTOR_PAGE_SIZE)
        self.page = max(0, min(page, self.total_pages - 1))

        options = self._page_options()
        if options:
            self.add_item(EventManageSelect(self, options))

        if self.total_pages > ADAPTIVE_JUMP_THRESHOLD:
            self.first_button = discord.ui.Button(label=FIRST_PAGE_LABEL, style=discord.ButtonStyle.secondary)
            self.first_button.callback = self.first_page
            self.first_button.disabled = self.page <= 0
            self.add_item(self.first_button)
        else:
            self.first_button = None

        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.total_pages - 1

        if self.total_pages > ADAPTIVE_JUMP_THRESHOLD:
            self.last_button = discord.ui.Button(label=LAST_PAGE_LABEL, style=discord.ButtonStyle.secondary)
            self.last_button.callback = self.last_page
            self.last_button.disabled = self.page >= self.total_pages - 1
            self.add_item(self.last_button)
        else:
            self.last_button = None

    def _page_slice(self) -> list[dict]:
        start = self.page * EVENT_SELECTOR_PAGE_SIZE
        end = start + EVENT_SELECTOR_PAGE_SIZE
        return self.cog.events[start:end]

    def _page_options(self) -> list[discord.SelectOption]:
        start = self.page * EVENT_SELECTOR_PAGE_SIZE
        options: list[discord.SelectOption] = []
        for index, event in enumerate(self._page_slice(), start=start + 1):
            description = f"{self.cog.describe_event_type(event)} - {self.cog.describe_event_status(event, self.guild)}"
            options.append(
                discord.SelectOption(
                    label=f"{index:02d} {event['name']}"[:100],
                    value=event["key"],
                    description=description[:100],
                )
            )
        return options

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Manage Events",
            description="Choose an event to edit.",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=format_page_footer(self.page + 1, self.total_pages))
        return embed

    async def first_page(self, interaction: discord.Interaction) -> None:
        view = EventManageSelectView(self.cog, self.guild, page=0)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    @discord.ui.button(label=PREV_PAGE_LABEL, style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = EventManageSelectView(self.cog, self.guild, page=max(0, self.page - 1))
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    @discord.ui.button(label=NEXT_PAGE_LABEL, style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = EventManageSelectView(self.cog, self.guild, page=min(self.total_pages - 1, self.page + 1))
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def last_page(self, interaction: discord.Interaction) -> None:
        view = EventManageSelectView(self.cog, self.guild, page=self.total_pages - 1)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    @discord.ui.button(label="Overview", style=discord.ButtonStyle.secondary)
    async def panel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await edit_bound_view(
            interaction,
            embed=self.cog.build_panel_embed(self.guild),
            view=EventPanelView(self.cog, self.guild),
        )

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Event panel closed.", embed=None, view=None)


class EventActionView(BaseTimeoutView):
    def __init__(self, cog: "EventStatsCog", guild: discord.Guild | None, key: str, *, return_page: int = 0):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild = guild
        self.key = key
        self.return_page = return_page

        event = self.cog.get_event(key)
        ordered_keys = [item["key"] for item in self.cog.events]
        event_index = ordered_keys.index(key) if key in ordered_keys else -1
        self.move_up.disabled = event_index <= 0
        self.move_down.disabled = event_index < 0 or event_index >= len(ordered_keys) - 1

        if event is not None:
            self.toggle.label = "Disable" if event.get("enabled", True) else "Enable"
            self.delete_or_reset.label = "Reset" if event.get("source") == "preset" else "Delete"
        else:
            self.edit.disabled = True
            self.toggle.disabled = True
            self.category.disabled = True
            self.move_up.disabled = True
            self.move_down.disabled = True
            self.delete_or_reset.disabled = True

    def build_embed(self) -> discord.Embed:
        return self.cog.build_event_detail_embed(self.guild, self.key)

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary, row=0)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _ensure_manage_permission(self.cog, interaction):
            return
        event = self.cog.get_event(self.key)
        if event is None:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return
        if event.get("source") == "custom":
            await interaction.response.send_modal(OneTimeEventModal(self.cog, self.guild, event_key=self.key))
            return
        await interaction.response.send_modal(PresetEventModal(self.cog, self.guild, event_key=self.key))

    @discord.ui.button(label="Disable", style=discord.ButtonStyle.secondary, row=0)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _ensure_manage_permission(self.cog, interaction):
            return
        if self.guild is None:
            await interaction.response.send_message(
                "I couldn't reach the server right now. Try again in a moment.",
                ephemeral=True,
            )
            return
        if not self.cog.toggle_event(self.key):
            await interaction.response.send_message("That event no longer exists.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.cog.force_refresh(self.guild)
        refreshed = EventActionView(self.cog, self.guild, self.key, return_page=self.return_page)
        self.stop()
        await edit_original_bound_view(interaction, embed=refreshed.build_embed(), view=refreshed)

    @discord.ui.button(label="Category", style=discord.ButtonStyle.secondary, row=0)
    async def category(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _ensure_manage_permission(self.cog, interaction):
            return
        view = EventCategoryView(self.cog, self.guild, self.key, return_page=self.return_page)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    @discord.ui.button(label="Move Up", style=discord.ButtonStyle.secondary, row=1)
    async def move_up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _ensure_manage_permission(self.cog, interaction):
            return
        if self.guild is None:
            await interaction.response.send_message(
                "I couldn't reach the server right now. Try again in a moment.",
                ephemeral=True,
            )
            return
        if not self.cog.move_event(self.key, -1):
            await interaction.response.send_message("I couldn't move that event. Try again in a moment.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.cog.force_refresh(self.guild)
        refreshed = EventActionView(self.cog, self.guild, self.key, return_page=self.return_page)
        self.stop()
        await edit_original_bound_view(interaction, embed=refreshed.build_embed(), view=refreshed)

    @discord.ui.button(label="Move Down", style=discord.ButtonStyle.secondary, row=1)
    async def move_down(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _ensure_manage_permission(self.cog, interaction):
            return
        if self.guild is None:
            await interaction.response.send_message(
                "I couldn't reach the server right now. Try again in a moment.",
                ephemeral=True,
            )
            return
        if not self.cog.move_event(self.key, 1):
            await interaction.response.send_message("I couldn't move that event. Try again in a moment.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.cog.force_refresh(self.guild)
        refreshed = EventActionView(self.cog, self.guild, self.key, return_page=self.return_page)
        self.stop()
        await edit_original_bound_view(interaction, embed=refreshed.build_embed(), view=refreshed)

    @discord.ui.button(label="Reset", style=discord.ButtonStyle.danger, row=1)
    async def delete_or_reset(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _ensure_manage_permission(self.cog, interaction):
            return
        event = self.cog.get_event(self.key)
        if event is None:
            await interaction.response.send_message("That event no longer exists.", ephemeral=True)
            return
        if event.get("source") == "preset":
            if self.guild is None:
                await interaction.response.send_message(
                    "I couldn't reach the server right now. Try again in a moment.",
                    ephemeral=True,
                )
                return
            if not self.cog.reset_preset_event(self.key):
                await interaction.response.send_message("I couldn't reset that event. Try again in a moment.", ephemeral=True)
                return
            await interaction.response.defer()
            await self.cog.force_refresh(self.guild)
            refreshed = EventActionView(self.cog, self.guild, self.key, return_page=self.return_page)
            self.stop()
            await edit_original_bound_view(interaction, embed=refreshed.build_embed(), view=refreshed)
            return

        view = EventDeleteConfirmView(self.cog, self.guild, self.key, return_page=self.return_page)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = EventManageSelectView(self.cog, self.guild, page=self.return_page)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    @discord.ui.button(label="Update", style=discord.ButtonStyle.secondary, row=2)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _ensure_manage_permission(self.cog, interaction):
            return
        if self.guild is None:
            await interaction.response.send_message(
                "I couldn't reach the server right now. Try again in a moment.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        await self.cog.force_refresh(self.guild)
        refreshed = EventActionView(self.cog, self.guild, self.key, return_page=self.return_page)
        self.stop()
        await edit_original_bound_view(interaction, embed=refreshed.build_embed(), view=refreshed)


class EventCategorySelect(discord.ui.ChannelSelect):
    def __init__(self, view: "EventCategoryView"):
        super().__init__(
            channel_types=[discord.ChannelType.category],
            min_values=1,
            max_values=1,
            placeholder="Choose a category",
        )
        self._parent_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_manage_permission(self._parent_view.cog, interaction):
            return
        if self._parent_view.guild is None:
            await interaction.response.send_message(
                "I couldn't reach the server right now. Try again in a moment.",
                ephemeral=True,
            )
            return
        category = self.values[0]
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("Choose a category channel.", ephemeral=True)
            return
        if not self._parent_view.cog.set_event_category(self._parent_view.key, category.id):
            await interaction.response.send_message("That event no longer exists.", ephemeral=True)
            return
        await interaction.response.defer()
        await self._parent_view.cog.force_refresh(self._parent_view.guild)
        refreshed = EventActionView(
            self._parent_view.cog,
            self._parent_view.guild,
            self._parent_view.key,
            return_page=self._parent_view.return_page,
        )
        self._parent_view.stop()
        await edit_original_bound_view(interaction, embed=refreshed.build_embed(), view=refreshed)


class EventCategoryView(BaseTimeoutView):
    def __init__(self, cog: "EventStatsCog", guild: discord.Guild | None, key: str, *, return_page: int = 0):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild = guild
        self.key = key
        self.return_page = return_page
        self.add_item(EventCategorySelect(self))

    def build_embed(self) -> discord.Embed:
        embed = self.cog.build_event_detail_embed(self.guild, self.key)
        embed.title = f"{embed.title} - Category"
        embed.description = (
            f"{embed.description}\n\nChoose a category, or clear the current one."
        )
        return embed

    @discord.ui.button(label="Clear Category", style=discord.ButtonStyle.secondary)
    async def clear_category(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _ensure_manage_permission(self.cog, interaction):
            return
        if self.guild is None:
            await interaction.response.send_message(
                "I couldn't reach the server right now. Try again in a moment.",
                ephemeral=True,
            )
            return
        if not self.cog.set_event_category(self.key, None):
            await interaction.response.send_message("That event no longer exists.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.cog.force_refresh(self.guild)
        refreshed = EventActionView(self.cog, self.guild, self.key, return_page=self.return_page)
        self.stop()
        await edit_original_bound_view(interaction, embed=refreshed.build_embed(), view=refreshed)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = EventActionView(self.cog, self.guild, self.key, return_page=self.return_page)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Event panel closed.", embed=None, view=None)


class EventDeleteConfirmView(BaseTimeoutView):
    def __init__(self, cog: "EventStatsCog", guild: discord.Guild | None, key: str, *, return_page: int = 0):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.key = key
        self.return_page = return_page

    def build_embed(self) -> discord.Embed:
        embed = self.cog.build_event_detail_embed(self.guild, self.key)
        embed.title = f"{embed.title} - Confirm Delete"
        embed.description = (
            f"{embed.description}\n\nThis will also delete the voice channel that "
            "displays this event's stats."
        )
        return embed

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def confirm_delete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _ensure_manage_permission(self.cog, interaction):
            return
        if self.guild is None:
            await interaction.response.send_message(
                "I couldn't reach the server right now. Try again in a moment.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        ok, message = await self.cog.delete_custom_event(self.guild, self.key)
        if ok:
            await self.cog.force_refresh(self.guild)
            self.stop()
            await edit_original_bound_view(
                interaction,
                content=message,
                embed=self.cog.build_panel_embed(self.guild),
                view=EventPanelView(self.cog, self.guild),
            )
            return
        await interaction.edit_original_response(content=message, embed=self.build_embed(), view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_delete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = EventActionView(self.cog, self.guild, self.key, return_page=self.return_page)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)


class OneTimeEventModal(BaseErrorModal):
    def __init__(self, cog: "EventStatsCog", guild: discord.Guild | None, *, event_key: str | None = None):
        title = "Create One-Time Event" if event_key is None else "Edit One-Time Event"
        super().__init__(title=title)
        self.cog = cog
        self.guild = guild
        self.event_key = event_key

        event = self.cog.get_event(event_key) if event_key else None
        timezone_text = str(event.get("timezone") or "UTC") if event else "UTC"
        tzinfo = resolve_timezone_input(timezone_text)

        start_default = ""
        end_default = ""
        if event and tzinfo is not None:
            start_default = format_event_datetime_local(event["start"], tzinfo)
            end_default = format_event_datetime_local(event["end"], tzinfo)

        self.name_input = discord.ui.TextInput(
            label="Event Name",
            required=True,
            max_length=MAX_EVENT_NAME_LENGTH,
            default=event["name"] if event else None,
            placeholder="CWL Push",
        )
        self.start_input = discord.ui.TextInput(
            label="Start",
            required=True,
            default=start_default or None,
            placeholder="YYYY-MM-DD HH:MM",
        )
        self.end_input = discord.ui.TextInput(
            label="End",
            required=True,
            default=end_default or None,
            placeholder="YYYY-MM-DD HH:MM",
        )
        self.timezone_input = discord.ui.TextInput(
            label="Timezone",
            required=True,
            default=timezone_text,
            placeholder="UTC, Paris, Europe/Paris, New York",
            max_length=40,
        )
        self.grace_input = discord.ui.TextInput(
            label="Grace Period (Hours)",
            required=False,
            default=str(int(event.get("grace_period_hours", DEFAULT_GRACE_HOURS))) if event else str(DEFAULT_GRACE_HOURS),
            placeholder="24",
            max_length=3,
        )

        self.add_item(self.name_input)
        self.add_item(self.start_input)
        self.add_item(self.end_input)
        self.add_item(self.timezone_input)
        self.add_item(self.grace_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.cog._can_manage(interaction.user):
            await interaction.response.send_message("You don't have permission to manage events.", ephemeral=True)
            return

        event_name = self.name_input.value.strip()
        if not event_name:
            await interaction.response.send_message("Enter an event name.", ephemeral=True)
            return

        timezone_name = self.timezone_input.value.strip()
        tzinfo = resolve_timezone_input(timezone_name)
        if tzinfo is None:
            await interaction.response.send_message(
                "That timezone wasn't recognized. Try a city name like Paris or a zone like Europe/Paris.",
                ephemeral=True,
            )
            return

        grace_hours = _parse_grace_hours(self.grace_input.value)
        if grace_hours is None:
            await interaction.response.send_message(
                f"Grace hours must be a whole number between 0 and {MAX_GRACE_HOURS}.",
                ephemeral=True,
            )
            return

        start = parse_event_datetime_input(self.start_input.value, tzinfo)
        end = parse_event_datetime_input(self.end_input.value, tzinfo)
        if start is None or end is None:
            await interaction.response.send_message(
                "That date/time wasn't recognized. Try YYYY-MM-DD HH:MM — for example, 2025-12-01 14:00.",
                ephemeral=True,
            )
            return
        if end <= start:
            await interaction.response.send_message("End time must be after the start time.", ephemeral=True)
            return

        timezone_value = canonical_timezone_name(timezone_name) or "UTC"
        await interaction.response.defer(ephemeral=True)
        if self.event_key is None:
            event_key = self.cog.create_one_time_event(
                name=event_name,
                start=start,
                end=end,
                timezone=timezone_value,
                grace_hours=grace_hours,
            )
        else:
            updated = self.cog.update_one_time_event(
                self.event_key,
                name=event_name,
                start=start,
                end=end,
                timezone=timezone_value,
                grace_hours=grace_hours,
            )
            if not updated:
                await interaction.followup.send("That event no longer exists.", ephemeral=True)
                return
            event_key = self.event_key

        if self.guild is not None:
            await self.cog.force_refresh(self.guild)

        view = EventActionView(self.cog, self.guild, event_key)
        message = await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True, wait=True)
        view.bind_message(message)


class PresetEventModal(BaseErrorModal):
    def __init__(self, cog: "EventStatsCog", guild: discord.Guild | None, *, event_key: str):
        super().__init__(title="Edit Recurring Event")
        self.cog = cog
        self.guild = guild
        self.event_key = event_key

        event = self.cog.get_event(event_key)
        self.name_input = discord.ui.TextInput(
            label="Display Name",
            required=True,
            max_length=MAX_EVENT_NAME_LENGTH,
            default=event["name"] if event else None,
        )
        self.add_item(self.name_input)

        if event and event["type"] != "counter" and not (event["type"] == "recurring" and event.get("schedule_shape") == "point"):
            self.grace_input = discord.ui.TextInput(
                label="Grace Period (Hours)",
                required=False,
                default=str(int(event.get("grace_period_hours", DEFAULT_GRACE_HOURS))),
                placeholder="24",
                max_length=3,
            )
            self.add_item(self.grace_input)
        else:
            self.grace_input = None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.cog._can_manage(interaction.user):
            await interaction.response.send_message("You don't have permission to manage events.", ephemeral=True)
            return

        display_name = self.name_input.value.strip()
        if not display_name:
            await interaction.response.send_message("Enter a display name.", ephemeral=True)
            return

        grace_hours = None
        if self.grace_input is not None:
            grace_hours = _parse_grace_hours(self.grace_input.value)
            if grace_hours is None:
                await interaction.response.send_message(
                    f"Grace hours must be a whole number between 0 and {MAX_GRACE_HOURS}.",
                    ephemeral=True,
                )
                return

        updated = self.cog.update_preset_event(
            self.event_key,
            name=display_name,
            grace_hours=grace_hours,
        )
        if not updated:
            await interaction.response.send_message("That event no longer exists.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        if self.guild is not None:
            await self.cog.force_refresh(self.guild)

        view = EventActionView(self.cog, self.guild, self.event_key)
        message = await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True, wait=True)
        view.bind_message(message)

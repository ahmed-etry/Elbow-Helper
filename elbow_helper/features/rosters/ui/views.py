"""Discord components for roster signups and lead-only management."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from elbow_helper.discord.interactions import deny
from elbow_helper.discord.interactions import warn
from elbow_helper.discord.pagination import ADAPTIVE_JUMP_THRESHOLD
from elbow_helper.discord.pagination import FIRST_PAGE_LABEL
from elbow_helper.discord.pagination import format_page_footer
from elbow_helper.discord.pagination import LAST_PAGE_LABEL
from elbow_helper.discord.pagination import NEXT_PAGE_LABEL
from elbow_helper.discord.pagination import PREV_PAGE_LABEL
from elbow_helper.discord.views import BaseErrorModal
from elbow_helper.discord.views import BaseTimeoutView
from ..config import ROSTER_DISCORD_COLUMN_MAX_WIDTH
from ..config import ROSTER_DISCORD_COLUMN_MIN_WIDTH
from ..config import ROSTER_PLAYER_COLUMN_MAX_WIDTH
from ..config import ROSTER_PLAYER_COLUMN_MIN_WIDTH
from ..config import ROSTER_SELECTOR_PAGE_SIZE
from ..models import LinkedAccount
from ..models import RosterLayout
from ..models import RosterMember

if TYPE_CHECKING:
    from .cog import Rosters


ROSTER_LAYOUT_PROMPT = (
    "Choose which columns appear in Discord. The Player column is always shown. Google Sheets "
    "keeps every column."
)


def _join_labels(labels: list[str]) -> str:
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return f"{', '.join(labels[:-1])}, and {labels[-1]}"


def roster_layout_columns_feedback(layout: RosterLayout) -> str:
    columns: list[str] = []
    if layout.show_townhall:
        columns.append("Town Hall")
    columns.append("Player")
    if layout.show_discord:
        columns.append("Discord username")
    if layout.show_clan:
        columns.append("Clan")
    return (
        f"Roster now shows {_join_labels(columns)}.\n"
        "Google Sheets keeps every column."
    )


def roster_layout_lengths_feedback(layout: RosterLayout) -> str:
    return (
        f"Name lengths updated: Player {layout.player_width}, "
        f"Discord {layout.discord_width}.\n"
        "Google Sheets keeps every column."
    )


class RosterMessageView(BaseTimeoutView):
    def __init__(
        self,
        cog: "Rosters",
        roster_id: int,
        *,
        is_open: bool = True,
        buttons_hidden: bool = False,
        page: int = 0,
        page_count: int = 1,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.roster_id = roster_id

        refresh = discord.ui.Button(
            emoji="🔁",
            style=discord.ButtonStyle.secondary,
            custom_id=f"roster:refresh:{roster_id}",
            row=0,
        )
        refresh.callback = self.refresh
        self.add_item(refresh)

        if not buttons_hidden:
            signup = discord.ui.Button(
                label="Signup",
                style=discord.ButtonStyle.success,
                custom_id=f"roster:signup:{roster_id}",
                disabled=not is_open,
                row=0,
            )
            signup.callback = self.signup
            self.add_item(signup)

            opt_out = discord.ui.Button(
                label="Opt-out",
                style=discord.ButtonStyle.danger,
                custom_id=f"roster:optout:{roster_id}",
                disabled=not is_open,
                row=0,
            )
            opt_out.callback = self.opt_out
            self.add_item(opt_out)

        settings = discord.ui.Button(
            emoji="⚙️",
            style=discord.ButtonStyle.secondary,
            custom_id=f"roster:settings:{roster_id}",
            row=0,
        )
        settings.callback = self.settings
        self.add_item(settings)

        if page_count > 1:
            if page_count > ADAPTIVE_JUMP_THRESHOLD:
                first = discord.ui.Button(
                    label=FIRST_PAGE_LABEL,
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"roster:first:{roster_id}",
                    disabled=page <= 0,
                    row=1,
                )
                first.callback = self.first_page
                self.add_item(first)

            previous = discord.ui.Button(
                label=PREV_PAGE_LABEL,
                style=discord.ButtonStyle.secondary,
                custom_id=f"roster:previous:{roster_id}",
                disabled=page <= 0,
                row=1,
            )
            previous.callback = self.previous_page
            self.add_item(previous)

            self.add_item(
                discord.ui.Button(
                    label=format_page_footer(page + 1, page_count),
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"roster:page:{roster_id}",
                    disabled=True,
                    row=1,
                )
            )

            following = discord.ui.Button(
                label=NEXT_PAGE_LABEL,
                style=discord.ButtonStyle.secondary,
                custom_id=f"roster:next:{roster_id}",
                disabled=page >= page_count - 1,
                row=1,
            )
            following.callback = self.next_page
            self.add_item(following)

            if page_count > ADAPTIVE_JUMP_THRESHOLD:
                last = discord.ui.Button(
                    label=LAST_PAGE_LABEL,
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"roster:last:{roster_id}",
                    disabled=page >= page_count - 1,
                    row=1,
                )
                last.callback = self.last_page
                self.add_item(last)

    async def refresh(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_refresh(interaction, self.roster_id)

    async def signup(self, interaction: discord.Interaction) -> None:
        await self.cog.show_account_picker(interaction, self.roster_id, mode="signup")

    async def opt_out(self, interaction: discord.Interaction) -> None:
        await self.cog.show_account_picker(interaction, self.roster_id, mode="remove")

    async def settings(self, interaction: discord.Interaction) -> None:
        if not self.cog.is_lead(interaction.user):
            await deny(interaction, action="manage this roster")
            return
        await self.cog.show_settings(interaction, self.roster_id)

    async def first_page(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_page(interaction, self.roster_id, "first")

    async def previous_page(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_page(interaction, self.roster_id, "previous")

    async def next_page(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_page(interaction, self.roster_id, "next")

    async def last_page(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_page(interaction, self.roster_id, "last")


class AccountSelect(discord.ui.Select):
    def __init__(
        self,
        accounts: list[LinkedAccount],
        *,
        mode: str,
        selected_tags: set[str] | None = None,
        row: int = 0,
    ):
        selected = selected_tags or set()
        options = []
        for account in accounts[:ROSTER_SELECTOR_PAGE_SIZE]:
            clan = account.clan_code or "No clan"
            th = f"TH{account.townhall}" if account.townhall else "TH unknown"
            details = f"{th} • {clan}"
            options.append(
                discord.SelectOption(
                    label=f"{account.player_name} ({account.player_tag})"[:100],
                    value=account.player_tag,
                    description=details[:100],
                    default=account.player_tag in selected,
                )
            )
        super().__init__(
            placeholder=(
                "Select accounts to remove"
                if mode == "remove"
                else "Select accounts"
            ),
            min_values=1,
            max_values=len(options),
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, AccountPickerView):
            if view.lead_override:
                await view.select_accounts(interaction, list(self.values))
            else:
                await view.submit(interaction, list(self.values))


class AccountPickerMemberSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Select another member",
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, AccountPickerView):
            return
        member = self.values[0]
        await view.cog.show_account_picker(
            interaction,
            view.roster_id,
            mode=view.mode,
            member_id=member.id,
            lead_override=True,
            edit_response=True,
        )


class AccountPickerView(BaseTimeoutView):
    def __init__(
        self,
        cog: "Rosters",
        roster_id: int,
        *,
        member_id: int,
        accounts: list[LinkedAccount],
        mode: str,
        lead_override: bool,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.roster_id = roster_id
        self.member_id = member_id
        self.mode = mode
        self.lead_override = lead_override
        accounts = sorted(
            accounts,
            key=lambda account: (
                -account.townhall,
                -account.hero_sum,
                account.player_name.casefold(),
                account.player_tag,
            ),
        )
        self.accounts = {account.player_tag: account for account in accounts}
        self.selected_tags: set[str] = set()
        if lead_override:
            self.add_item(AccountPickerMemberSelect())
        self.account_select = AccountSelect(
            accounts,
            mode=mode,
            row=1 if lead_override else 0,
        )
        self.add_item(self.account_select)
        if lead_override:
            self.confirm_button = discord.ui.Button(
                label="Add",
                style=discord.ButtonStyle.success,
                disabled=True,
                row=2,
            )
            self.confirm_button.callback = self.confirm
            self.add_item(self.confirm_button)
            self.deselect_button = discord.ui.Button(
                label="Deselect",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                row=2,
            )
            self.deselect_button.callback = self.deselect
            self.add_item(self.deselect_button)
            bulk_add = discord.ui.Button(
                label="Bulk add",
                style=discord.ButtonStyle.secondary,
                row=2,
            )
            bulk_add.callback = self.bulk_add
            self.add_item(bulk_add)

    async def select_accounts(
        self,
        interaction: discord.Interaction,
        player_tags: list[str],
    ) -> None:
        self.selected_tags = set(player_tags)
        for option in self.account_select.options:
            option.default = option.value in self.selected_tags
        self.confirm_button.disabled = not self.selected_tags
        self.deselect_button.disabled = not self.selected_tags
        await interaction.response.edit_message(view=self)

    async def deselect(self, interaction: discord.Interaction) -> None:
        self.selected_tags.clear()
        for option in self.account_select.options:
            option.default = False
        self.confirm_button.disabled = True
        self.deselect_button.disabled = True
        await interaction.response.edit_message(view=self)

    async def bulk_add(self, interaction: discord.Interaction) -> None:
        if not self.cog.is_lead(interaction.user):
            await deny(interaction, action="manage this roster")
            return
        await interaction.response.send_modal(
            BulkRosterAddModal(self.cog, self.roster_id)
        )

    async def confirm(self, interaction: discord.Interaction) -> None:
        await self.submit(interaction, sorted(self.selected_tags))

    async def submit(self, interaction: discord.Interaction, player_tags: list[str]) -> None:
        if interaction.user.id != self.member_id and not (
            self.lead_override and self.cog.is_lead(interaction.user)
        ):
            await deny(interaction, action="change these signups")
            return
        await self.cog.apply_account_selection(
            interaction,
            self.roster_id,
            member_id=self.member_id,
            player_tags=player_tags,
            mode=self.mode,
            account_snapshots=self.accounts,
            bypass_min_townhall=self.lead_override,
        )


class RosterRemovalSelect(discord.ui.Select):
    def __init__(
        self,
        members: list[RosterMember],
        display_names: dict[int, str],
        selected_tags: set[str],
    ):
        options = []
        for member in members:
            discord_name = display_names.get(
                member.discord_user_id,
                str(member.discord_user_id),
            )
            clan = member.clan_code or "No clan"
            townhall = f"TH{member.townhall}" if member.townhall else "TH unknown"
            options.append(
                discord.SelectOption(
                    label=f"{member.player_name} ({member.player_tag})"[:100],
                    value=member.player_tag,
                    description=f"{discord_name} • {clan} • {townhall}"[:100],
                    default=member.player_tag in selected_tags,
                )
            )
        super().__init__(
            placeholder="Select accounts to remove",
            min_values=0,
            max_values=len(options),
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, RosterRemovalView):
            await view.update_selection(interaction, list(self.values))


class RosterRemovalMemberSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Filter by member",
            min_values=0,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, RosterRemovalView):
            member_id = self.values[0].id if self.values else None
            await view.filter_member(interaction, member_id)


class RosterRemovalView(BaseTimeoutView):
    def __init__(
        self,
        cog: "Rosters",
        roster_id: int,
        members: list[RosterMember],
        display_names: dict[int, str],
        *,
        page: int = 0,
        selected_tags: set[str] | None = None,
        member_filter_id: int | None = None,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.roster_id = roster_id
        self.members = members
        self.display_names = display_names
        self.selected_tags = set(selected_tags or ())
        self.member_filter_id = member_filter_id
        self.filtered_members = (
            [member for member in members if member.discord_user_id == member_filter_id]
            if member_filter_id is not None
            else members
        )
        self.add_item(RosterRemovalMemberSelect())
        self.page_count = max(
            1,
            (len(self.filtered_members) + ROSTER_SELECTOR_PAGE_SIZE - 1)
            // ROSTER_SELECTOR_PAGE_SIZE,
        )
        self.page = min(max(page, 0), self.page_count - 1)
        start = self.page * ROSTER_SELECTOR_PAGE_SIZE
        if self.filtered_members:
            self.add_item(
                RosterRemovalSelect(
                    self.filtered_members[start:start + ROSTER_SELECTOR_PAGE_SIZE],
                    display_names,
                    self.selected_tags,
                )
            )

        if self.page_count > 1:
            if self.page_count > ADAPTIVE_JUMP_THRESHOLD:
                first = discord.ui.Button(
                    label=FIRST_PAGE_LABEL,
                    style=discord.ButtonStyle.secondary,
                    disabled=self.page == 0,
                    row=2,
                )
                first.callback = self.first_page
                self.add_item(first)
            previous = discord.ui.Button(
                label=PREV_PAGE_LABEL,
                style=discord.ButtonStyle.secondary,
                disabled=self.page == 0,
                row=2,
            )
            previous.callback = self.previous_page
            self.add_item(previous)
            self.add_item(
                discord.ui.Button(
                    label=format_page_footer(self.page + 1, self.page_count),
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                    row=2,
                )
            )
            following = discord.ui.Button(
                label=NEXT_PAGE_LABEL,
                style=discord.ButtonStyle.secondary,
                disabled=self.page >= self.page_count - 1,
                row=2,
            )
            following.callback = self.next_page
            self.add_item(following)
            if self.page_count > ADAPTIVE_JUMP_THRESHOLD:
                last = discord.ui.Button(
                    label=LAST_PAGE_LABEL,
                    style=discord.ButtonStyle.secondary,
                    disabled=self.page >= self.page_count - 1,
                    row=2,
                )
                last.callback = self.last_page
                self.add_item(last)

        remove = discord.ui.Button(
            label="Remove",
            style=discord.ButtonStyle.danger,
            disabled=not self.selected_tags,
            row=3,
        )
        remove.callback = self.confirm
        self.add_item(remove)
        deselect = discord.ui.Button(
            label="Deselect",
            style=discord.ButtonStyle.secondary,
            disabled=not self.selected_tags and self.member_filter_id is None,
            row=3,
        )
        deselect.callback = self.deselect
        self.add_item(deselect)

    async def update_selection(
        self,
        interaction: discord.Interaction,
        player_tags: list[str],
    ) -> None:
        start = self.page * ROSTER_SELECTOR_PAGE_SIZE
        page_tags = {
            member.player_tag
            for member in self.filtered_members[start:start + ROSTER_SELECTOR_PAGE_SIZE]
        }
        selected = self.selected_tags - page_tags
        selected.update(player_tags)
        await interaction.response.edit_message(
            view=RosterRemovalView(
                self.cog,
                self.roster_id,
                self.members,
                self.display_names,
                page=self.page,
                selected_tags=selected,
                member_filter_id=self.member_filter_id,
            )
        )

    async def filter_member(
        self,
        interaction: discord.Interaction,
        member_id: int | None,
    ) -> None:
        selected = self.selected_tags
        if member_id is not None:
            allowed = {
                member.player_tag
                for member in self.members
                if member.discord_user_id == member_id
            }
            if not allowed:
                await interaction.response.edit_message(
                    content="That member has no signups.",
                    view=RosterRemovalView(
                        self.cog,
                        self.roster_id,
                        self.members,
                        self.display_names,
                        selected_tags=selected,
                    ),
                )
                return
            selected &= allowed
        view = RosterRemovalView(
            self.cog,
            self.roster_id,
            self.members,
            self.display_names,
            selected_tags=selected,
            member_filter_id=member_id,
        )
        await interaction.response.edit_message(
            content=None,
            view=view,
        )

    async def confirm(self, interaction: discord.Interaction) -> None:
        await self.cog.remove_roster_players(
            interaction,
            self.roster_id,
            sorted(self.selected_tags),
        )

    async def deselect(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content=None,
            view=RosterRemovalView(
                self.cog,
                self.roster_id,
                self.members,
                self.display_names,
                page=self.page,
            )
        )

    async def first_page(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, 0)

    async def previous_page(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, self.page - 1)

    async def next_page(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, self.page + 1)

    async def last_page(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, self.page_count - 1)

    async def _change_page(
        self,
        interaction: discord.Interaction,
        page: int,
    ) -> None:
        await interaction.response.edit_message(
            view=RosterRemovalView(
                self.cog,
                self.roster_id,
                self.members,
                self.display_names,
                page=page,
                selected_tags=self.selected_tags,
                member_filter_id=self.member_filter_id,
            )
        )


class RosterSettingsSelect(discord.ui.Select):
    def __init__(self, *, is_open: bool, buttons_hidden: bool):
        state_action = "close" if is_open else "open"
        super().__init__(
            placeholder="Select an action",
            options=[
                discord.SelectOption(
                    label="Export to Google Sheets",
                    value="export",
                    description="Create or update the signup sheet.",
                ),
                discord.SelectOption(
                    label=f"{state_action.title()} roster",
                    value=state_action,
                    description=(
                        "Prevent new signups." if is_open else "Allow new signups."
                    ),
                ),
                discord.SelectOption(
                    label="Clear signups",
                    value="clear",
                    description="Remove every current signup.",
                ),
                discord.SelectOption(
                    label="Show buttons" if buttons_hidden else "Hide buttons",
                    value="toggle_buttons",
                    description=(
                        "Show Signup and Opt-out."
                        if buttons_hidden
                        else "Hide Signup and Opt-out."
                    ),
                ),
                discord.SelectOption(
                    label="Layout",
                    value="layout",
                    description="Choose what appears and when names are shortened.",
                ),
                discord.SelectOption(
                    label="Add accounts",
                    value="add",
                    description="Add accounts for a member.",
                ),
                discord.SelectOption(
                    label="Remove accounts",
                    value="remove",
                    description="Remove accounts from the roster.",
                ),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, RosterSettingsView):
            await view.run_action(interaction, self.values[0])


class RosterSettingsView(BaseTimeoutView):
    def __init__(
        self,
        cog: "Rosters",
        roster_id: int,
        *,
        is_open: bool,
        buttons_hidden: bool,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.roster_id = roster_id
        self.add_item(
            RosterSettingsSelect(
                is_open=is_open,
                buttons_hidden=buttons_hidden,
            )
        )

    async def run_action(self, interaction: discord.Interaction, action: str) -> None:
        if not self.cog.is_lead(interaction.user):
            await deny(interaction, action="manage this roster")
            return
        if action == "add":
            await interaction.response.edit_message(
                content=None,
                view=RosterTargetMemberView(self.cog, self.roster_id, mode=action),
            )
            return
        if action == "remove":
            await self.cog.show_roster_removal_picker(interaction, self.roster_id)
            return
        if action == "layout":
            await self.cog.show_roster_layout(interaction, self.roster_id)
            return
        await self.cog.handle_management_action(interaction, self.roster_id, action)


class RosterLayoutSelect(discord.ui.Select):
    def __init__(self, layout: RosterLayout):
        super().__init__(
            placeholder="Choose columns to show",
            min_values=0,
            max_values=3,
            options=[
                discord.SelectOption(
                    label="Town Hall",
                    value="townhall",
                    default=layout.show_townhall,
                ),
                discord.SelectOption(
                    label="Discord username",
                    value="discord",
                    default=layout.show_discord,
                ),
                discord.SelectOption(
                    label="Clan",
                    value="clan",
                    default=layout.show_clan,
                ),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, RosterLayoutView):
            await view.cog.update_roster_layout_columns(
                interaction,
                view.roster_id,
                set(self.values),
            )


class RosterColumnWidthsModal(BaseErrorModal, title="Set roster name lengths"):
    def __init__(self, cog: "Rosters", roster_id: int, layout: RosterLayout):
        super().__init__()
        self.cog = cog
        self.roster_id = roster_id
        self.player_width = discord.ui.TextInput(
            label=(
                f"Player names ({ROSTER_PLAYER_COLUMN_MIN_WIDTH}–"
                f"{ROSTER_PLAYER_COLUMN_MAX_WIDTH} characters)"
            ),
            default=str(layout.player_width),
            min_length=1,
            max_length=2,
        )
        self.discord_width = discord.ui.TextInput(
            label=(
                f"Discord usernames ({ROSTER_DISCORD_COLUMN_MIN_WIDTH}–"
                f"{ROSTER_DISCORD_COLUMN_MAX_WIDTH} characters)"
            ),
            default=str(layout.discord_width),
            min_length=1,
            max_length=2,
        )
        self.add_item(self.player_width)
        self.add_item(self.discord_width)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            player_width = int(str(self.player_width.value))
        except ValueError:
            await warn(
                interaction,
                f"Enter a whole number from {ROSTER_PLAYER_COLUMN_MIN_WIDTH} to "
                f"{ROSTER_PLAYER_COLUMN_MAX_WIDTH} for player names.",
            )
            return
        if not ROSTER_PLAYER_COLUMN_MIN_WIDTH <= player_width <= ROSTER_PLAYER_COLUMN_MAX_WIDTH:
            await warn(
                interaction,
                f"Enter a whole number from {ROSTER_PLAYER_COLUMN_MIN_WIDTH} to "
                f"{ROSTER_PLAYER_COLUMN_MAX_WIDTH} for player names.",
            )
            return
        try:
            discord_width = int(str(self.discord_width.value))
        except ValueError:
            await warn(
                interaction,
                f"Enter a whole number from {ROSTER_DISCORD_COLUMN_MIN_WIDTH} to "
                f"{ROSTER_DISCORD_COLUMN_MAX_WIDTH} for Discord usernames.",
            )
            return
        if not ROSTER_DISCORD_COLUMN_MIN_WIDTH <= discord_width <= ROSTER_DISCORD_COLUMN_MAX_WIDTH:
            await warn(
                interaction,
                f"Enter a whole number from {ROSTER_DISCORD_COLUMN_MIN_WIDTH} to "
                f"{ROSTER_DISCORD_COLUMN_MAX_WIDTH} for Discord usernames.",
            )
            return
        await self.cog.update_roster_layout_widths(
            interaction,
            self.roster_id,
            player_width=player_width,
            discord_width=discord_width,
        )


class RosterLayoutView(BaseTimeoutView):
    def __init__(self, cog: "Rosters", roster_id: int, layout: RosterLayout):
        super().__init__(timeout=180)
        self.cog = cog
        self.roster_id = roster_id
        self.layout = layout
        self.add_item(RosterLayoutSelect(layout))

        widths = discord.ui.Button(
            label="Edit name lengths",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        widths.callback = self.edit_widths
        self.add_item(widths)

        back = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        back.callback = self.back
        self.add_item(back)

    async def edit_widths(self, interaction: discord.Interaction) -> None:
        if not self.cog.is_lead(interaction.user):
            await deny(interaction, action="manage this roster")
            return
        await interaction.response.send_modal(
            RosterColumnWidthsModal(self.cog, self.roster_id, self.layout)
        )

    async def back(self, interaction: discord.Interaction) -> None:
        if not self.cog.is_lead(interaction.user):
            await deny(interaction, action="manage this roster")
            return
        await self.cog.show_roster_settings(interaction, self.roster_id)


class RosterProgressView(discord.ui.View):
    """A single disabled control that makes an in-progress action visible."""

    def __init__(self, label: str):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                disabled=True,
            )
        )


class BulkRosterAddModal(discord.ui.Modal, title="Bulk add accounts"):
    player_tags = discord.ui.TextInput(
        label="Player tags",
        placeholder="#PYLQ2 #G8R9V",
        style=discord.TextStyle.paragraph,
        max_length=2000,
    )

    def __init__(self, cog: "Rosters", roster_id: int):
        super().__init__()
        self.cog = cog
        self.roster_id = roster_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.bulk_add_roster_accounts(
            interaction,
            self.roster_id,
            str(self.player_tags.value),
        )


class RosterTargetMemberSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Select a member", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, RosterTargetMemberView):
            member = self.values[0]
            await view.cog.show_account_picker(
                interaction,
                view.roster_id,
                mode="signup" if view.mode == "add" else "remove",
                member_id=member.id,
                lead_override=True,
                edit_response=True,
            )


class RosterTargetMemberView(BaseTimeoutView):
    def __init__(self, cog: "Rosters", roster_id: int, *, mode: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.roster_id = roster_id
        self.mode = mode
        self.add_item(RosterTargetMemberSelect())
        bulk_add = discord.ui.Button(
            label="Bulk add",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        bulk_add.callback = self.bulk_add
        self.add_item(bulk_add)

    async def bulk_add(self, interaction: discord.Interaction) -> None:
        if not self.cog.is_lead(interaction.user):
            await deny(interaction, action="manage this roster")
            return
        await interaction.response.send_modal(BulkRosterAddModal(self.cog, self.roster_id))


class ConfirmDeleteView(BaseTimeoutView):
    def __init__(self, cog: "Rosters", roster_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.roster_id = roster_id

    @discord.ui.button(label="Delete roster", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.cog.is_lead(interaction.user):
            await deny(interaction, action="delete this roster")
            return
        await self.cog.confirm_delete(interaction, self.roster_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Roster was not deleted.", view=None)


class ConfirmClearView(BaseTimeoutView):
    def __init__(self, cog: "Rosters", roster_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.roster_id = roster_id

    @discord.ui.button(label="Clear current signups", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.cog.is_lead(interaction.user):
            await deny(interaction, action="manage this roster")
            return
        await self.cog.confirm_clear(interaction, self.roster_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="No signups were cleared.", view=None)

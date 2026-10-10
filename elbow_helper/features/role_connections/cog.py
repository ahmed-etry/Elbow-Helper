"""Cog lifecycle, rule evaluation, board rendering, and scan entrypoints."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import time
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple

import discord
from elbow_helper.discord.application_emojis import loading_status
from discord import app_commands
from elbow_helper.discord.pagination import format_page_footer
from discord.ext import commands
from elbow_helper.discord.interactions import deny
from elbow_helper.discord.interactions import send_bound_view
from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import LEAD
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL

from .config import CONNECTIONS_PER_PAGE
from .config import ROLE_CHANGE_DELAY_SECONDS
from .config import SCAN_PREVIEW_PER_PAGE
from .config import SELECTOR_PAGE_SIZE
from .formatting import _conditions_to_lines
from .formatting import _removal_conditions_to_lines
from .formatting import _role_mention
from .state import load_state
from .state import save_state
from .board import ConnectionsView
from .scan import ScanConfirmView
from .queries import RoleConnectionQueries, invalid_connection_indexes

LOGGER = logging.getLogger(__name__)


class RoleConnections(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.state = load_state()
        self.queries = RoleConnectionQueries(
            lambda: self.state.get("connections", ()),
        )
        self._role_change_tasks: Dict[int, asyncio.Task] = {}
        self._scan_lock = asyncio.Lock()
        self._scan_tasks: set[asyncio.Task] = set()
        self._invalid_connection_signature: tuple[str, ...] = ()

    @staticmethod
    def _invalid_connection_indexes(
        connections: List[Dict[str, Any]],
    ) -> set[int]:
        return invalid_connection_indexes(connections)

    def connection_change_is_valid(
        self,
        connection: Dict[str, Any],
        *,
        replacing_id: str | None = None,
    ) -> bool:
        """Check a proposed rule against every managed-role dependency."""

        prospective = [
            copy.deepcopy(existing)
            for existing in self.state["connections"]
            if replacing_id is None or existing.get("id") != replacing_id
        ]
        prospective.append(copy.deepcopy(connection))
        return not self._invalid_connection_indexes(prospective)

    def cog_unload(self) -> None:
        for task in self._role_change_tasks.values():
            if not task.done():
                task.cancel()
        for task in tuple(self._scan_tasks):
            if not task.done():
                task.cancel()

    def new_connection_id(self) -> str:
        return f"rc_{int(time.time() * 1000)}"

    def _can_manage(self, member: discord.Member | discord.User) -> bool:
        if not isinstance(member, discord.Member):
            return False
        return any(r.id in LEAD for r in getattr(member, "roles", []))

    def _replace_connections(self, connections: List[Dict[str, Any]]) -> None:
        next_state = copy.deepcopy(self.state)
        next_state["connections"] = connections
        save_state(next_state)
        self.state = next_state

    def add_connection(self, connection: Dict[str, Any]) -> None:
        connections = copy.deepcopy(self.state["connections"])
        connections.append(copy.deepcopy(connection))
        self._replace_connections(connections)

    def remove_connection(self, conn_id: str) -> bool:
        connections = copy.deepcopy(self.state["connections"])
        for idx, connection in enumerate(connections):
            if connection["id"] == conn_id:
                connections.pop(idx)
                self._replace_connections(connections)
                return True
        return False

    def _track_role_change_task(self, member_id: int, task: asyncio.Task) -> None:
        self._role_change_tasks[member_id] = task

        def _cleanup(completed: asyncio.Task) -> None:
            current = self._role_change_tasks.get(member_id)
            if current is completed:
                self._role_change_tasks.pop(member_id, None)

        task.add_done_callback(_cleanup)

    def get_connections_page_count(self) -> int:
        total = len(self.state["connections"])
        return max(1, math.ceil(total / CONNECTIONS_PER_PAGE))

    def get_selector_page_count(self) -> int:
        total = len(self.state["connections"])
        return max(1, math.ceil(total / SELECTOR_PAGE_SIZE))

    def get_scan_preview_page_count(self) -> int:
        total = len(self.state["connections"])
        return max(1, math.ceil(total / SCAN_PREVIEW_PER_PAGE))

    def get_connection(self, conn_id: str) -> Optional[Dict[str, Any]]:
        for connection in self.state["connections"]:
            if connection["id"] == conn_id:
                return connection
        return None

    def role_connection_state(self, conn_id: str) -> Optional[Dict[str, Any]]:
        """Return a detached connection for previews and later rechecks."""
        connection = self.get_connection(conn_id)
        return copy.deepcopy(connection) if connection is not None else None

    def replace_connection(self, conn_id: str, connection: Dict[str, Any]) -> bool:
        """Save one complete rule after validating its dependencies."""
        if connection.get("id") != conn_id or not self.connection_change_is_valid(
            connection, replacing_id=conn_id,
        ):
            return False
        connections = copy.deepcopy(self.state["connections"])
        for index, existing in enumerate(connections):
            if existing["id"] == conn_id:
                connections[index] = copy.deepcopy(connection)
                self._replace_connections(connections)
                return True
        return False

    def update_connection_target(self, conn_id: str, role_id: int) -> bool:
        connection = self.role_connection_state(conn_id)
        if connection is None:
            return False
        connection["target_role_id"] = role_id
        return self.replace_connection(conn_id, connection)

    def get_connection_list_ids(self, connection: Dict[str, Any], list_name: str, kind: str) -> List[int]:
        key = "has" if kind == "has" else "not"
        return [cond[key] for cond in connection.get(list_name, []) if key in cond]

    def add_connection_roles(self, conn_id: str, list_name: str, kind: str, role_ids: List[int]) -> bool:
        connection = self.role_connection_state(conn_id)
        if connection is None:
            return False
        key = "has" if kind == "has" else "not"
        target = connection.get(list_name, [])
        for role_id in role_ids:
            entry = {key: role_id}
            if entry not in target:
                target.append(entry)
        connection[list_name] = target
        return self.replace_connection(conn_id, connection)

    def remove_connection_roles(self, conn_id: str, list_name: str, kind: str, role_ids: List[int]) -> bool:
        connection = self.role_connection_state(conn_id)
        if connection is None:
            return False
        key = "has" if kind == "has" else "not"
        target = connection.get(list_name, [])
        connection[list_name] = [cond for cond in target if cond.get(key) not in role_ids]
        return self.replace_connection(conn_id, connection)

    def _connection_matches(self, member: discord.Member, connection: Dict[str, Any]) -> bool:
        # `all` must fully match; `any` acts as an optional OR gate.
        return self._connection_matches_ids({r.id for r in member.roles}, connection)

    @staticmethod
    def _connection_matches_ids(member_role_ids: set[int], connection: Dict[str, Any]) -> bool:
        all_conditions = connection.get("all", [])
        for cond in all_conditions:
            if "has" in cond and cond["has"] not in member_role_ids:
                return False
            if "not" in cond and cond["not"] in member_role_ids:
                return False
        any_conditions = connection.get("any", [])
        if any_conditions:
            matched = False
            for cond in any_conditions:
                if "has" in cond and cond["has"] in member_role_ids:
                    matched = True
                    break
                if "not" in cond and cond["not"] not in member_role_ids:
                    matched = True
                    break
            if not matched:
                return False
        return True

    async def role_connection_scan_plan(self, guild: discord.Guild):
        """Calculate ordered role changes before a panel or agent scan."""
        try:
            members = [member async for member in guild.fetch_members(limit=None)]
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Falling back to guild member cache during role connection scan.")
            members = list(guild.members)
        connections = self.state["connections"]
        invalid = self._invalid_connection_indexes(connections)
        plan = []
        for member in members:
            member_roles = {role.id for role in member.roles}
            changes = []
            for index, connection in enumerate(connections):
                if index in invalid:
                    continue
                role = guild.get_role(connection["target_role_id"])
                if role is None:
                    continue
                add = self._connection_matches_ids(member_roles, connection)
                if (role.id in member_roles) == add:
                    continue
                changes.append((role, add))
                if add:
                    member_roles.add(role.id)
                else:
                    member_roles.remove(role.id)
            plan.append((member, tuple(changes)))
        return tuple(plan)

    async def apply_role_connection_change(self, member: discord.Member,
                                           role: discord.Role, *, add: bool) -> bool:
        """Apply one scanned role change for the panel and agent."""
        try:
            if add:
                await member.add_roles(role, reason="Apply role connections")
            else:
                await member.remove_roles(role, reason="Apply role connections")
        except (discord.Forbidden, discord.HTTPException):
            return False
        return True

    async def _apply_connections_to_member(self, member: discord.Member, reason: str) -> Tuple[int, int]:
        added = 0
        removed = 0
        connections = self.state["connections"]
        invalid_indexes = self._invalid_connection_indexes(connections)
        invalid_signature = tuple(
            str(connections[index].get("id") or index)
            for index in sorted(invalid_indexes)
        )
        if invalid_signature and invalid_signature != getattr(
            self,
            "_invalid_connection_signature",
            (),
        ):
            LOGGER.error(
                "Skipping cyclic role connections: %s",
                ", ".join(invalid_signature),
            )
        self._invalid_connection_signature = invalid_signature
        for index, connection in enumerate(connections):
            if index in invalid_indexes:
                continue
            role = member.guild.get_role(connection["target_role_id"])
            if not role:
                continue
            should_have = self._connection_matches(member, connection)
            has_role = role in member.roles
            try:
                if should_have and not has_role:
                    await member.add_roles(role, reason=reason)
                    added += 1
                elif not should_have and has_role:
                    await member.remove_roles(role, reason=reason)
                    removed += 1
            except (discord.Forbidden, discord.HTTPException):
                continue
        return added, removed

    async def post_connections_message(self, channel: discord.TextChannel) -> discord.Message:
        embed = self.build_connections_embed()
        view = ConnectionsView(self)
        message = await channel.send(embed=embed, view=view)
        view.bind_message(message)
        return message

    def connections_board_signature(self) -> str:
        return json.dumps(self.state["connections"], sort_keys=True)

    async def refresh_connections_message(
        self,
        fallback_channel: discord.TextChannel,
    ) -> discord.Message:
        return await self.post_connections_message(fallback_channel)

    def build_connections_embed(self, page: int = 0) -> discord.Embed:
        embed = discord.Embed(
            title="Role Connections",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        connections = self.state["connections"]
        if not connections:
            embed.description = "No role connections are set up yet."
            return embed
        total_pages = self.get_connections_page_count()
        page = max(0, min(page, total_pages - 1))
        start = page * CONNECTIONS_PER_PAGE
        end = start + CONNECTIONS_PER_PAGE
        for idx, connection in enumerate(connections[start:end], start=start + 1):
            target_role = _role_mention(connection["target_role_id"])
            lines = _conditions_to_lines(connection)
            if lines:
                condition_text = "\n".join(f"- {line}" for line in lines)
            else:
                condition_text = "No conditions added yet."
            value = (
                f"Role managed: {target_role}\n"
                "Conditions:\n"
                f"{condition_text}"
            )
            embed.add_field(name=f"Connection {idx}", value=value, inline=False)
        if total_pages > 1:
            embed.set_footer(text=format_page_footer(page + 1, total_pages))
        return embed

    def build_scan_preview_embed(self, page: int = 0) -> discord.Embed:
        embed = discord.Embed(
            title="Apply Role Connections",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        if not self.state["connections"]:
            embed.description = "No role connections are set up yet."
            return embed
        total_pages = self.get_scan_preview_page_count()
        page = max(0, min(page, total_pages - 1))
        start = page * SCAN_PREVIEW_PER_PAGE
        end = start + SCAN_PREVIEW_PER_PAGE
        for idx, connection in enumerate(self.state["connections"][start:end], start=start + 1):
            target = _role_mention(connection["target_role_id"])
            add_lines = _conditions_to_lines(connection)
            remove_lines = _removal_conditions_to_lines(connection)
            add_text = "\n".join(f"- {line}" for line in add_lines) if add_lines else "- No conditions — this role is always added."
            remove_text = "\n".join(f"- {line}" for line in remove_lines) if remove_lines else "- Never removed while this connection exists."
            value = (
                f"Role managed: {target}\n"
                "Added when:\n"
                f"{add_text}\n"
                "\n"
                "Removed when:\n"
                f"{remove_text}"
            )
            embed.add_field(name=f"Connection {idx}", value=value, inline=False)
        if total_pages > 1:
            embed.set_footer(text=format_page_footer(page + 1, total_pages))
        return embed

    async def post_scan_preview(self, interaction: discord.Interaction) -> None:
        embed = self.build_scan_preview_embed(page=0)
        view = ScanConfirmView(self, page=0)
        await send_bound_view(interaction, embed=embed, view=view)

    async def run_scan(self, message: discord.Message) -> None:
        async with self._scan_lock:
            guild = message.guild or self.bot.get_guild(GUILD_ID)
            if not guild:
                return
            plan = await self.role_connection_scan_plan(guild)
            if not plan:
                return
            total = len(plan)
            added_total = 0
            removed_total = 0
            processed = 0
            last_update = time.monotonic()

            for member, changes in plan:
                for role, add in changes:
                    changed = await self.apply_role_connection_change(member, role, add=add)
                    if changed and add:
                        added_total += 1
                    elif changed:
                        removed_total += 1
                processed += 1
                if processed == total or (time.monotonic() - last_update) >= 2:
                    progress = discord.Embed(
                        title="Applying Role Connections",
                        description=await loading_status(
                            self.bot, f"Checking members: {processed}/{total}",
                        ),
                        color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                        timestamp=datetime.now(timezone.utc),
                    )
                    progress.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
                    try:
                        await message.edit(content=None, embed=progress, view=None)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        LOGGER.debug("Failed updating scan progress message")
                    last_update = time.monotonic()

            summary = discord.Embed(
                title="Role Updates Complete",
                description=f"Added roles: {added_total}\nRemoved roles: {removed_total}",
                color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                timestamp=datetime.now(timezone.utc),
            )
            summary.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
            try:
                await message.edit(content=None, embed=summary, view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.debug("Failed posting scan summary")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles == after.roles:
            return
        existing = self._role_change_tasks.get(after.id)
        if existing and not existing.done():
            existing.cancel()
        task = asyncio.create_task(self._delayed_apply(after.id))
        self._track_role_change_task(after.id, task)

    async def _delayed_apply(self, member_id: int) -> None:
        # Let clustered role updates settle before re-evaluating connections.
        await asyncio.sleep(ROLE_CHANGE_DELAY_SECONDS)
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return
        member = guild.get_member(member_id)
        if member:
            await self._apply_connections_to_member(member, reason="Update role connections")

    @app_commands.command(name="connections", description="Manage rules that add or remove member roles.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def connections(self, interaction: discord.Interaction):
        if not self._can_manage(interaction.user):
            await deny(interaction)
            return
        channel = interaction.channel
        if not channel or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Use this in a server channel.", ephemeral=True)
            return
        message = await self.post_connections_message(channel)
        await interaction.response.send_message(
            f"Role connections board posted: {message.jump_url}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RoleConnections(bot))




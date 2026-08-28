"""Interactive views for `/health settings`."""

from __future__ import annotations

import copy
from typing import Any, Dict, List

import discord
from elbow_helper.discord.interactions import edit_bound_view, send_bound_view
from elbow_helper.discord.views import BaseErrorModal, BaseTimeoutView

from elbow_helper.configuration.clans import CLAN_NAMES
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL

from ..config import CLAN_PROFILE_BY_CODE
from ..config_labels import PLAYER_BLOCK_ORDER, PLAYER_LABELS, PROFILE_JUDGMENT_TEXT
from ..database.config_store import (
    ConfigConflictError,
    ConfigValidationError,
    get_player_config,
    get_player_config_with_meta,
    get_player_template_payload,
    save_player_config,
)
from ..player_health_config import load_player_health_config


def _get_payload(clan_code: str) -> Dict[str, Any]:
    return get_player_config(clan_code) or get_player_template_payload(clan_code)


def _get_payload_with_snapshot(clan_code: str) -> tuple[Dict[str, Any], str | None]:
    result = get_player_config_with_meta(clan_code)
    return result if result is not None else (get_player_template_payload(clan_code), None)


def _field_keys(block_labels: Dict[str, Dict[str, Any]]) -> List[str]:
    return [key for key in block_labels.keys() if not key.startswith("_")]


def _format_value(value: Any, spec: Dict[str, Any]) -> str:
    value_type = str(spec.get("type") or "int")
    unit = str(spec.get("unit") or "").strip().lower()
    if value_type == "float":
        return f"{float(value):g}"
    number = int(value)
    if unit in {"gold", "points"}:
        return f"{number:,}"
    if unit == "%":
        return f"{number}%"
    return str(number)


def _parse_value(raw: str, spec: Dict[str, Any]) -> Any:
    label = str(spec.get("label") or "Value")
    unit = str(spec.get("unit") or "").strip().lower()
    value_text = str(raw or "").strip().replace(",", "")
    if unit == "%" and value_text.endswith("%"):
        value_text = value_text[:-1].strip()
    if not value_text:
        raise ValueError(f"{label} cannot be blank.")
    value_type = str(spec.get("type") or "int")
    if value_type == "int":
        if any(ch in value_text for ch in (".", "e", "E")):
            raise ValueError(f"{label} must be a whole number.")
        value = int(value_text)
    else:
        value = float(value_text)
    if value < 0:
        raise ValueError(f"{label} cannot be negative.")
    if spec.get("max") is not None and value > spec["max"]:
        raise ValueError(f"{label} must be at most {spec['max']}.")
    return value


def _config_errors() -> List[str]:
    config, errors = load_player_health_config()
    if config is None and errors:
        return ["Some Clan Health settings couldn't be loaded. Standard settings are being used instead."]
    return []


def _profile_name(clan_code: str) -> str:
    return str(CLAN_PROFILE_BY_CODE.get(clan_code, "casual") or "casual").strip().lower()


def _judgment_lines(clan_code: str) -> List[str]:
    profile_name = _profile_name(clan_code)
    return list(PROFILE_JUDGMENT_TEXT.get(profile_name, PROFILE_JUDGMENT_TEXT["casual"]))


def _guardrail_errors(payload: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    war = payload.get("war") or {}
    clan_games = payload.get("clan_games") or {}

    if int(war.get("wars_to_join") or 0) > 14:
        errors.append("The number of wars is higher than a member could realistically join.")
    if int(clan_games.get("minimum_points_per_event") or 0) > 5000:
        errors.append("Clan Games points cannot exceed the event maximum.")
    return errors


def _block_title(block: str) -> str:
    return str(PLAYER_LABELS[block].get("_title") or block.replace("_", " ").title())


def _block_help(block: str) -> str:
    return str(PLAYER_LABELS[block].get("_help") or "")


def _block_options() -> List[discord.SelectOption]:
    return [
        discord.SelectOption(
            label=str(PLAYER_LABELS[block].get("_title") or block)[:100],
            value=block,
            description=" ".join(str(PLAYER_LABELS[block].get("_help") or "").split())[:100],
        )
        for block in PLAYER_BLOCK_ORDER
    ]


def _block_summary_lines(block: str, payload: Dict[str, Any]) -> List[str]:
    block_labels = PLAYER_LABELS[block]
    block_payload = payload.get(block, {})
    lines: List[str] = []
    for key in _field_keys(block_labels):
        spec = block_labels[key]
        value = _format_value(block_payload[key], spec)
        lines.append(f"**{spec.get('label')}:** `{value}`")
    return lines


class EditBlockModal(BaseErrorModal):
    def __init__(self, parent_view: "ClanConfigBlockView"):
        super().__init__(title=f"Edit {parent_view.block_title}")
        self.parent_view = parent_view
        self._snapshot_updated_at = parent_view._snapshot_updated_at
        self.labels = PLAYER_LABELS[parent_view.block]
        self.payload = _get_payload(parent_view.clan_code)
        block_payload = self.payload[parent_view.block]
        self.inputs_by_key: Dict[str, discord.ui.TextInput] = {}
        for key in _field_keys(self.labels):
            spec = self.labels[key]
            control = discord.ui.TextInput(
                label=str(spec.get("label") or key)[:45],
                default=_format_value(block_payload[key], spec),
                required=True,
                max_length=32,
            )
            self.inputs_by_key[key] = control
            self.add_item(control)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        block_payload = copy.deepcopy(self.payload[self.parent_view.block])
        parse_errors: List[str] = []
        for key in _field_keys(self.labels):
            spec = self.labels[key]
            try:
                block_payload[key] = _parse_value(self.inputs_by_key[key].value, spec)
            except (ValueError, TypeError) as exc:
                parse_errors.append(str(exc))
        if parse_errors:
            await interaction.response.send_message("Couldn't save:\n- " + "\n- ".join(parse_errors), ephemeral=True)
            return

        new_payload = copy.deepcopy(self.payload)
        new_payload[self.parent_view.block] = block_payload
        errors = _guardrail_errors(new_payload)
        if errors:
            await interaction.response.send_message("Couldn't save:\n- " + "\n- ".join(errors), ephemeral=True)
            return
        try:
            save_player_config(
                self.parent_view.clan_code,
                new_payload,
                interaction.user,
                expected_updated_at=self._snapshot_updated_at,
            )
        except ConfigValidationError as exc:
            await interaction.response.send_message("Couldn't save:\n- " + "\n- ".join(exc.errors), ephemeral=True)
            return
        except ConfigConflictError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer()
        refreshed_view = await self.parent_view.refresh_message(payload_override=new_payload)
        if refreshed_view is not None:
            self.parent_view = refreshed_view


class BlockSelect(discord.ui.Select):
    def __init__(self, *, view: BaseTimeoutView, clan_code: str, current_block: str | None = None):
        options = _block_options()
        for option in options:
            option.default = option.value == current_block
        super().__init__(placeholder="Choose a settings section", min_values=1, max_values=1, options=options, row=0)
        self._parent_view = view
        self._clan_code = clan_code

    async def callback(self, interaction: discord.Interaction) -> None:
        block = self.values[0]
        view = ClanConfigBlockView(self._clan_code, block)
        self._parent_view.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)


class ClanConfigBlockView(BaseTimeoutView):
    def __init__(self, clan_code: str, block: str):
        super().__init__(timeout=900)
        self.clan_code = str(clan_code).upper()
        self.block = block
        _, self._snapshot_updated_at = _get_payload_with_snapshot(self.clan_code)
        self.block_labels = PLAYER_LABELS[block]
        self.block_title = _block_title(block)
        self._build_controls()

    def _build_controls(self) -> None:
        self.add_item(BlockSelect(view=self, clan_code=self.clan_code, current_block=self.block))

        edit_button = discord.ui.Button(label="Edit", style=discord.ButtonStyle.primary, row=1)

        async def edit_callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(EditBlockModal(self))

        edit_button.callback = edit_callback
        self.add_item(edit_button)

        back_button = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=1)

        async def back_callback(interaction: discord.Interaction) -> None:
            view = ClanConfigHomeView(self.clan_code)
            self.stop()
            await edit_bound_view(interaction, embed=view.build_embed(), view=view)

        back_button.callback = back_callback
        self.add_item(back_button)

    def build_embed(self, payload_override: Dict[str, Any] | None = None) -> discord.Embed:
        payload = payload_override if payload_override is not None else _get_payload(self.clan_code)
        clan_name = CLAN_NAMES.get(self.clan_code, self.clan_code)
        embed = discord.Embed(
            title=f"{self.block_title} - {clan_name}",
            description=_block_help(self.block),
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        for banner in _config_errors():
            embed.add_field(name="Warning", value=banner, inline=False)
        embed.add_field(name="Current settings", value="\n".join(_block_summary_lines(self.block, payload)), inline=False)
        return embed

    async def refresh_message(self, payload_override: Dict[str, Any] | None = None) -> "ClanConfigBlockView | None":
        if self.message is None:
            return None
        fresh = ClanConfigBlockView(self.clan_code, self.block)
        _, fresh._snapshot_updated_at = _get_payload_with_snapshot(self.clan_code)
        try:
            await self.message.edit(embed=fresh.build_embed(payload_override=payload_override), view=fresh)
        except (discord.NotFound, discord.HTTPException):
            return None
        fresh.bind_message(self.message)
        self.stop()
        self.message = None
        return fresh


class ClanConfigHomeView(BaseTimeoutView):
    def __init__(self, clan_code: str):
        super().__init__(timeout=900)
        self.clan_code = str(clan_code).upper()
        self._build_controls()

    def _build_controls(self) -> None:
        self.add_item(BlockSelect(view=self, clan_code=self.clan_code, current_block=None))

    def build_embed(self) -> discord.Embed:
        clan_name = CLAN_NAMES.get(self.clan_code, self.clan_code)
        payload = _get_payload(self.clan_code)
        embed = discord.Embed(
            title=f"Clan Health Settings — {clan_name}",
            description="Set member expectations.",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        for banner in _config_errors():
            embed.add_field(name="Warning", value=banner, inline=False)
        embed.add_field(name="About These Settings", value="\n".join(_judgment_lines(self.clan_code)), inline=False)
        for block in PLAYER_BLOCK_ORDER:
            embed.add_field(
                name=_block_title(block),
                value="\n".join(_block_summary_lines(block, payload)),
                inline=False,
            )
        return embed

    @classmethod
    async def open(cls, interaction: discord.Interaction, clan_code: str) -> "ClanConfigHomeView":
        view = cls(clan_code)
        await send_bound_view(interaction, embed=view.build_embed(), view=view, ephemeral=True)
        return view

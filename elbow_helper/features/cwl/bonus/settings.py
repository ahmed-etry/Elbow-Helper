"""Interactive CWL bonus scoring settings."""

from __future__ import annotations

import copy
import math
from datetime import datetime
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

import discord

from elbow_helper.discord.interactions import deny
from elbow_helper.discord.interactions import edit_bound_view
from elbow_helper.discord.interactions import send_bound_view
from elbow_helper.discord.pagination import ADAPTIVE_JUMP_THRESHOLD
from elbow_helper.discord.pagination import FIRST_PAGE_LABEL
from elbow_helper.discord.pagination import LAST_PAGE_LABEL
from elbow_helper.discord.pagination import NEXT_PAGE_LABEL
from elbow_helper.discord.pagination import PREV_PAGE_LABEL
from elbow_helper.discord.pagination import format_page_footer
from elbow_helper.discord.views import BaseErrorModal
from elbow_helper.discord.views import BaseTimeoutView
from elbow_helper.configuration.clans import CLAN_NAMES
from elbow_helper.configuration.roles import LEAD
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL

from ..config import CWL_CLAN_CODES
from .config import BonusConfigConflictError
from .config import BonusConfigValidationError


def _is_lead(cog: Any, interaction: discord.Interaction) -> bool:
    return bool(cog._has_any_role(interaction, set(LEAD)))


async def _deny_settings(interaction: discord.Interaction) -> None:
    await deny(interaction, action="edit CWL bonus scoring")


def _load_clan_snapshot(cog: Any, clan_code: str) -> tuple[Dict[str, Any], Dict[str, Any], int]:
    config, errors = cog.bonus_config.load()
    if config is None:
        raise BonusConfigValidationError(errors)
    payload = (config.get("clans") or {}).get(clan_code)
    if not isinstance(payload, dict):
        raise BonusConfigValidationError([f"CWL bonus scoring settings for {clan_code} aren't available. Check that clan's settings and try again."])
    meta = (config.get("clan_meta") or {}).get(clan_code) or {}
    return copy.deepcopy(payload), dict(meta), cog.bonus_config.revision(config)


def _number(value: Any) -> str:
    return f"{float(value):g}"


def _updated_line(meta: Dict[str, Any]) -> str:
    raw_timestamp = meta.get("updated_at_utc")
    actor = str(meta.get("updated_by_name") or "-")
    if not raw_timestamp:
        return "No changes recorded yet"
    try:
        timestamp = int(datetime.fromisoformat(str(raw_timestamp)).timestamp())
    except (TypeError, ValueError):
        return f"{raw_timestamp} by {actor}"
    return f"<t:{timestamp}:f> by {actor}"


def _expected_keys(payload: Dict[str, Any], attacker_th: Optional[int] = None) -> List[str]:
    matchup_expected = payload.get("matchup_expected") or {}
    keys: List[tuple[int, int, str]] = []
    for key in matchup_expected:
        try:
            attacker_raw, defender_raw = str(key).split(":", 1)
            attacker = int(attacker_raw)
            defender = int(defender_raw)
        except (TypeError, ValueError):
            continue
        if attacker_th is None or attacker == attacker_th:
            keys.append((attacker, defender, str(key)))
    keys.sort()
    return [key for _, _, key in keys]


def _settings_embed(title: str, description: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
    )
    embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
    return embed


class LeadBonusSettingsView(BaseTimeoutView):
    def __init__(self, *, timeout: float = 86400):
        super().__init__(timeout=timeout)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cog = getattr(self, "cog", None)
        if cog is not None and _is_lead(cog, interaction):
            return True
        await _deny_settings(interaction)
        return False


class BonusSettingsButton(discord.ui.Button):
    def __init__(self, cog: Any, selected_clans: List[str]):
        super().__init__(
            label="Scoring Settings",
            style=discord.ButtonStyle.secondary,
            custom_id="cwl_bonus_scoring_settings",
        )
        self.cog = cog
        self.selected_clans = list(selected_clans)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_lead(self.cog, interaction):
            await _deny_settings(interaction)
            return
        if len(self.selected_clans) == 1:
            await BonusSettingsHomeView.open(
                interaction,
                self.cog,
                self.selected_clans[0],
            )
            return
        await BonusSettingsClanPickerView.open(interaction, self.cog)


class BonusExportView(BaseTimeoutView):
    """Bonus export links and scoring-settings entry point."""

    def __init__(self):
        super().__init__(timeout=86400)

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, BonusSettingsButton):
                child.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass


class SettingsSectionSelect(discord.ui.Select):
    def __init__(self, parent: "BonusSettingsHomeView"):
        super().__init__(
            placeholder="Choose what to review",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Expected Scores",
                    value="expected",
                    description="Review the baseline for each Town Hall matchup.",
                ),
                discord.SelectOption(
                    label="TH Adjustments",
                    value="adjustments",
                    description="Review uphit credit and downhit penalties.",
                ),
                discord.SelectOption(
                    label="Copy From Clan",
                    value="copy",
                    description="Replace this setup with another clan's settings.",
                ),
                discord.SelectOption(
                    label="Change History",
                    value="history",
                    description="Review recent scoring changes.",
                ),
            ],
            row=0,
        )
        self.panel = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        selection = self.values[0]
        if selection == "expected":
            view: LeadBonusSettingsView = ExpectedScoresView(self.panel.cog, self.panel.clan_code)
        elif selection == "adjustments":
            view = AdjustmentsView(self.panel.cog, self.panel.clan_code)
        elif selection == "copy":
            view = CopyClanView(self.panel.cog, self.panel.clan_code)
        else:
            view = HistoryView(self.panel.cog, self.panel.clan_code)
        self.panel.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)


class BonusSettingsHomeView(LeadBonusSettingsView):
    def __init__(self, cog: Any, clan_code: str, *, notice: Optional[str] = None):
        super().__init__()
        self.cog = cog
        self.clan_code = str(clan_code).upper()
        self.notice = notice
        self.add_item(SettingsSectionSelect(self))

    def build_embed(self) -> discord.Embed:
        _, meta, _ = _load_clan_snapshot(self.cog, self.clan_code)
        embed = _settings_embed(
            f"CWL Bonus Scoring - {self.clan_code}",
            (
                f"Review how {self.clan_code} scores CWL attacks. "
                "Expected Scores set the baseline for each Town Hall matchup, while TH Adjustments "
                "account for attacking a higher or lower Town Hall.\n\n"
                "Saved changes apply to future reports only. Existing sheets will not change."
            ),
        )
        if self.notice:
            embed.add_field(name="Saved", value=self.notice, inline=False)
        embed.add_field(name="Last updated", value=_updated_line(meta), inline=False)
        return embed

    @classmethod
    async def open(
        cls,
        interaction: discord.Interaction,
        cog: Any,
        clan_code: str,
    ) -> "BonusSettingsHomeView":
        view = cls(cog, clan_code)
        await send_bound_view(
            interaction,
            embed=view.build_embed(),
            view=view,
            ephemeral=True,
        )
        return view


class ClanPickerSelect(discord.ui.Select):
    def __init__(self, parent: "BonusSettingsClanPickerView"):
        super().__init__(
            placeholder="Choose a clan",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=CLAN_NAMES.get(code, code)[:100],
                    value=code,
                    description=code,
                )
                for code in CWL_CLAN_CODES
            ],
        )
        self.panel = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        view = BonusSettingsHomeView(self.panel.cog, self.values[0])
        self.panel.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)


class BonusSettingsClanPickerView(LeadBonusSettingsView):
    def __init__(self, cog: Any):
        super().__init__()
        self.cog = cog
        self.add_item(ClanPickerSelect(self))

    @staticmethod
    def build_embed() -> discord.Embed:
        return _settings_embed(
            "CWL Bonus Scoring",
            "This report covers multiple clans. Choose a clan to review or update its scoring setup.",
        )

    @classmethod
    async def open(cls, interaction: discord.Interaction, cog: Any) -> "BonusSettingsClanPickerView":
        view = cls(cog)
        await send_bound_view(
            interaction,
            embed=view.build_embed(),
            view=view,
            ephemeral=True,
        )
        return view


def _back_button(parent: LeadBonusSettingsView, *, row: int = 2) -> discord.ui.Button:
    button = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=row)

    async def callback(interaction: discord.Interaction) -> None:
        view = BonusSettingsHomeView(parent.cog, parent.clan_code)
        parent.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    button.callback = callback
    return button


class AttackerThSelect(discord.ui.Select):
    def __init__(self, parent: "ExpectedScoresView", attacker_levels: List[int]):
        super().__init__(
            placeholder="Choose attacker Town Hall",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=f"TH{level}",
                    value=str(level),
                    default=level == parent.attacker_th,
                )
                for level in attacker_levels
            ],
            row=0,
        )
        self.panel = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        view = ExpectedScoresView(
            self.panel.cog,
            self.panel.clan_code,
            attacker_th=int(self.values[0]),
        )
        self.panel.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)


class DefenderThSelect(discord.ui.Select):
    def __init__(self, parent: "ExpectedScoresView", keys: List[str]):
        super().__init__(
            placeholder="Choose defender Town Hall to edit",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=f"TH{key.split(':', 1)[1]}",
                    value=key,
                    description=f"Current score: {_number(parent.payload['matchup_expected'][key])}",
                )
                for key in keys
            ],
            row=1,
        )
        self.panel = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ExpectedScoreModal(self.panel, self.values[0]))


class ExpectedScoresView(LeadBonusSettingsView):
    def __init__(
        self,
        cog: Any,
        clan_code: str,
        *,
        attacker_th: Optional[int] = None,
        notice: Optional[str] = None,
    ):
        super().__init__()
        self.cog = cog
        self.clan_code = str(clan_code).upper()
        self.payload, _, self.revision = _load_clan_snapshot(cog, self.clan_code)
        attacker_levels = sorted(set(int(value) for value in self.payload["attacker_th_levels"]))
        self.attacker_th = attacker_th if attacker_th in attacker_levels else None
        self.notice = notice
        self.add_item(AttackerThSelect(self, attacker_levels))
        if self.attacker_th is not None:
            keys = _expected_keys(self.payload, self.attacker_th)
            if keys:
                self.add_item(DefenderThSelect(self, keys))
        self.add_item(_back_button(self, row=2))

    def build_embed(self) -> discord.Embed:
        embed = _settings_embed(
            f"Expected Scores - {self.clan_code}",
            (
                "Expected Score is the baseline for an attack. The report compares the attack's "
                "Actual Score with this value before applying any TH Adjustment."
            ),
        )
        if self.notice:
            embed.add_field(name="Saved", value=self.notice, inline=False)
        if self.attacker_th is None:
            embed.add_field(
                name="Choose an attacker Town Hall",
                value="Select a Town Hall level to review its matchups.",
                inline=False,
            )
            return embed
        keys = _expected_keys(self.payload, self.attacker_th)
        lines = [
            f"TH{self.attacker_th} attacking TH{key.split(':', 1)[1]}: "
            f"`{_number(self.payload['matchup_expected'][key])}`"
            for key in keys
        ]
        embed.add_field(
            name=f"TH{self.attacker_th} matchups",
            value="\n".join(lines) or "No matchup scores have been set for this Town Hall.",
            inline=False,
        )
        return embed


def _parse_expected_score(raw: str) -> float:
    try:
        value = float(str(raw or "").strip())
    except ValueError as exc:
        raise ValueError("Enter an Expected Score from 0.00 to 3.00.") from exc
    if not 0 <= value <= 3:
        raise ValueError("Enter an Expected Score from 0.00 to 3.00.")
    if 0.5 <= value < 1.0:
        raise ValueError(
            "Scores from 0.50 to 0.99 are not possible because a 0-star attack cannot reach 50% destruction."
        )
    return round(value, 3)


class ExpectedScoreModal(BaseErrorModal):
    def __init__(self, parent: ExpectedScoresView, matchup_key: str):
        attacker, defender = matchup_key.split(":", 1)
        super().__init__(title=f"TH{attacker} attacking TH{defender}")
        self.panel = parent
        self.matchup_key = matchup_key
        self.score = discord.ui.TextInput(
            label="Expected Score (0.00 to 3.00)",
            default=_number(parent.payload["matchup_expected"][matchup_key]),
            required=True,
            max_length=8,
        )
        self.add_item(self.score)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_lead(self.panel.cog, interaction):
            await _deny_settings(interaction)
            return
        try:
            new_value = _parse_expected_score(self.score.value)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        old_value = float(self.panel.payload["matchup_expected"][self.matchup_key])
        view = ExpectedScoreConfirmView(
            self.panel,
            self.matchup_key,
            old_value,
            new_value,
        )
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)


class ExpectedScoreConfirmView(LeadBonusSettingsView):
    def __init__(
        self,
        parent: ExpectedScoresView,
        matchup_key: str,
        old_value: float,
        new_value: float,
    ):
        super().__init__()
        self.cog = parent.cog
        self.clan_code = parent.clan_code
        self.attacker_th = parent.attacker_th
        self.payload = copy.deepcopy(parent.payload)
        self.revision = parent.revision
        self.matchup_key = matchup_key
        self.old_value = old_value
        self.new_value = new_value
        save_button = discord.ui.Button(label="Save Change", style=discord.ButtonStyle.success)
        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        save_button.callback = self.save
        cancel_button.callback = self.cancel
        self.add_item(save_button)
        self.add_item(cancel_button)

    def build_embed(self) -> discord.Embed:
        attacker, defender = self.matchup_key.split(":", 1)
        return _settings_embed(
            "Confirm Expected Score",
            (
                f"Update the Expected Score for TH{attacker} attacking TH{defender}?\n\n"
                f"`{_number(self.old_value)} -> {_number(self.new_value)}`\n\n"
                f"This change will apply to future {self.clan_code} reports."
            ),
        )

    async def save(self, interaction: discord.Interaction) -> None:
        updated_payload = copy.deepcopy(self.payload)
        updated_payload["matchup_expected"][self.matchup_key] = self.new_value
        attacker, defender = self.matchup_key.split(":", 1)
        try:
            self.cog.bonus_config.save_clan(
                self.clan_code,
                updated_payload,
                interaction.user,
                expected_revision=self.revision,
                summary=(
                    f"TH{attacker} vs TH{defender} Expected Score: "
                    f"{_number(self.old_value)} -> {_number(self.new_value)}"
                ),
            )
        except BonusConfigConflictError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except BonusConfigValidationError as exc:
            await interaction.response.send_message("Couldn't save:\n- " + "\n- ".join(exc.errors), ephemeral=True)
            return
        view = ExpectedScoresView(
            self.cog,
            self.clan_code,
            attacker_th=self.attacker_th,
            notice=f"TH{attacker} attacking TH{defender}: `{_number(self.old_value)} -> {_number(self.new_value)}`",
        )
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def cancel(self, interaction: discord.Interaction) -> None:
        view = ExpectedScoresView(self.cog, self.clan_code, attacker_th=self.attacker_th)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)


ADJUSTMENT_FIELDS = (
    ("uphit_bonus_per_level", "Uphit credit per level"),
    ("downhit_penalty_per_level", "Downhit penalty per level"),
    ("downhit_severe_after", "Extra penalty begins after"),
    ("downhit_severe_base", "Extra penalty base"),
    ("downhit_severe_multiplier", "Growth multiplier"),
)


class AdjustmentsView(LeadBonusSettingsView):
    def __init__(self, cog: Any, clan_code: str, *, notice: Optional[str] = None):
        super().__init__()
        self.cog = cog
        self.clan_code = str(clan_code).upper()
        self.payload, _, self.revision = _load_clan_snapshot(cog, self.clan_code)
        self.notice = notice
        edit_button = discord.ui.Button(label="Edit", style=discord.ButtonStyle.primary, row=0)

        async def edit_callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(AdjustmentsModal(self))

        edit_button.callback = edit_callback
        self.add_item(edit_button)
        self.add_item(_back_button(self, row=0))

    def build_embed(self) -> discord.Embed:
        embed = _settings_embed(
            f"TH Adjustments - {self.clan_code}",
            (
                "TH Adjustments account for matchup difficulty after Actual Score has been compared "
                "with Expected Score. Uphits receive additional credit; downhits receive a penalty."
            ),
        )
        if self.notice:
            embed.add_field(name="Saved", value=self.notice, inline=False)
        embed.add_field(
            name="Standard adjustments",
            value=(
                f"**Uphit credit**\n`+{_number(self.payload['uphit_bonus_per_level'])}` per TH level\n\n"
                f"**Downhit penalty**\n`-{_number(self.payload['downhit_penalty_per_level'])}` per TH level"
            ),
            inline=False,
        )
        embed.add_field(
            name="Progressive downhit penalty",
            value=(
                f"**Begins after**\n`{int(self.payload['downhit_severe_after'])}` TH levels\n\n"
                f"**Base penalty**\n`{_number(self.payload['downhit_severe_base'])}`\n\n"
                f"**Growth multiplier**\n`{_number(self.payload['downhit_severe_multiplier'])}x`"
            ),
            inline=False,
        )
        return embed


class AdjustmentsModal(BaseErrorModal):
    def __init__(self, parent: AdjustmentsView):
        super().__init__(title=f"Edit {parent.clan_code} TH Adjustments")
        self.panel = parent
        self.inputs: Dict[str, discord.ui.TextInput] = {}
        for key, label in ADJUSTMENT_FIELDS:
            control = discord.ui.TextInput(
                label=label,
                default=_number(parent.payload[key]),
                required=True,
                max_length=12,
            )
            self.inputs[key] = control
            self.add_item(control)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_lead(self.panel.cog, interaction):
            await _deny_settings(interaction)
            return
        values: Dict[str, Any] = {}
        errors: List[str] = []
        for key, label in ADJUSTMENT_FIELDS:
            raw = str(self.inputs[key].value or "").strip()
            try:
                value: Any = int(raw) if key == "downhit_severe_after" else float(raw)
            except ValueError:
                errors.append(f"{label} must be a number.")
                continue
            if isinstance(value, float) and not math.isfinite(value):
                errors.append(f"{label} must be a finite number.")
                continue
            if value < 0:
                errors.append(f"{label} cannot be negative.")
            if key == "downhit_severe_multiplier" and value < 1:
                errors.append("Growth multiplier must be at least 1.")
            values[key] = value
        if errors:
            await interaction.response.send_message("Couldn't continue:\n- " + "\n- ".join(errors), ephemeral=True)
            return
        view = AdjustmentsConfirmView(self.panel, values)
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)


class AdjustmentsConfirmView(LeadBonusSettingsView):
    def __init__(self, parent: AdjustmentsView, values: Dict[str, Any]):
        super().__init__()
        self.cog = parent.cog
        self.clan_code = parent.clan_code
        self.payload = copy.deepcopy(parent.payload)
        self.revision = parent.revision
        self.values = values
        self.changes = [
            (key, label, self.payload[key], values[key])
            for key, label in ADJUSTMENT_FIELDS
            if float(self.payload[key]) != float(values[key])
        ]
        save_button = discord.ui.Button(
            label="Save Changes",
            style=discord.ButtonStyle.success,
            disabled=not self.changes,
        )
        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        save_button.callback = self.save
        cancel_button.callback = self.cancel
        self.add_item(save_button)
        self.add_item(cancel_button)

    def build_embed(self) -> discord.Embed:
        lines = [
            f"**{label}:** `{_number(old)} -> {_number(new)}`"
            for _, label, old, new in self.changes
        ]
        return _settings_embed(
            "Confirm TH Adjustments",
            "Review the changes below before saving.\n\n" + ("\n".join(lines) if lines else "No values changed."),
        )

    async def save(self, interaction: discord.Interaction) -> None:
        updated_payload = copy.deepcopy(self.payload)
        updated_payload.update(self.values)
        summary = "; ".join(
            f"{label}: {_number(old)} -> {_number(new)}"
            for _, label, old, new in self.changes
        )
        try:
            self.cog.bonus_config.save_clan(
                self.clan_code,
                updated_payload,
                interaction.user,
                expected_revision=self.revision,
                summary=summary,
            )
        except BonusConfigConflictError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except BonusConfigValidationError as exc:
            await interaction.response.send_message("Couldn't save:\n- " + "\n- ".join(exc.errors), ephemeral=True)
            return
        view = AdjustmentsView(
            self.cog,
            self.clan_code,
            notice="TH Adjustments were updated. Existing sheets were not changed.",
        )
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def cancel(self, interaction: discord.Interaction) -> None:
        view = AdjustmentsView(self.cog, self.clan_code)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)


class CopyClanSelect(discord.ui.Select):
    def __init__(self, parent: "CopyClanView"):
        super().__init__(
            placeholder="Choose a clan to copy from",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=CLAN_NAMES.get(code, code)[:100],
                    value=code,
                    description=code,
                )
                for code in CWL_CLAN_CODES
                if code != parent.clan_code
            ],
            row=0,
        )
        self.panel = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        view = CopyConfirmView(self.panel, self.values[0])
        self.panel.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)


class CopyClanView(LeadBonusSettingsView):
    def __init__(self, cog: Any, clan_code: str):
        super().__init__()
        self.cog = cog
        self.clan_code = str(clan_code).upper()
        self.add_item(CopyClanSelect(self))
        self.add_item(_back_button(self, row=1))

    def build_embed(self) -> discord.Embed:
        return _settings_embed(
            f"Copy Scoring Setup - {self.clan_code}",
            (
                f"Replace {self.clan_code}'s current Expected Scores and TH Adjustments with another "
                "clan's settings. The copy will be recorded in Change History."
            ),
        )


class CopyConfirmView(LeadBonusSettingsView):
    def __init__(self, parent: CopyClanView, source_clan: str):
        super().__init__()
        self.cog = parent.cog
        self.clan_code = parent.clan_code
        self.source_clan = str(source_clan).upper()
        target_payload, _, self.revision = _load_clan_snapshot(self.cog, self.clan_code)
        source_payload, _, _ = _load_clan_snapshot(self.cog, self.source_clan)
        target_scores = target_payload.get("matchup_expected") or {}
        source_scores = source_payload.get("matchup_expected") or {}
        self.expected_changes = sum(
            1 for key in set(target_scores) | set(source_scores) if target_scores.get(key) != source_scores.get(key)
        )
        adjustment_keys = [key for key, _ in ADJUSTMENT_FIELDS]
        self.adjustment_changes = sum(
            1 for key in adjustment_keys if target_payload.get(key) != source_payload.get(key)
        )
        confirm_button = discord.ui.Button(label="Confirm Copy", style=discord.ButtonStyle.danger)
        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        confirm_button.callback = self.confirm
        cancel_button.callback = self.cancel
        self.add_item(confirm_button)
        self.add_item(cancel_button)

    def build_embed(self) -> discord.Embed:
        return _settings_embed(
            "Confirm Scoring Setup Copy",
            (
                f"**Copy from:** `{self.source_clan}`\n"
                f"**Copy to:** `{self.clan_code}`\n\n"
                f"Expected Scores changed: `{self.expected_changes}`\n"
                f"TH Adjustment fields changed: `{self.adjustment_changes}`\n\n"
                f"This replaces {self.clan_code}'s current scoring setup."
            ),
        )

    async def confirm(self, interaction: discord.Interaction) -> None:
        try:
            self.cog.bonus_config.copy_clan(
                self.source_clan,
                self.clan_code,
                interaction.user,
                expected_revision=self.revision,
            )
        except BonusConfigConflictError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except BonusConfigValidationError as exc:
            await interaction.response.send_message("Couldn't copy:\n- " + "\n- ".join(exc.errors), ephemeral=True)
            return
        view = BonusSettingsHomeView(
            self.cog,
            self.clan_code,
            notice=f"Copied Expected Scores and TH Adjustments from {self.source_clan}.",
        )
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def cancel(self, interaction: discord.Interaction) -> None:
        view = CopyClanView(self.cog, self.clan_code)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)


class HistoryView(LeadBonusSettingsView):
    PAGE_SIZE = 8

    def __init__(self, cog: Any, clan_code: str, *, offset: int = 0):
        super().__init__()
        self.cog = cog
        self.clan_code = str(clan_code).upper()
        self.offset = max(0, int(offset))
        self.entries, self.total_entries = cog.bonus_config.history(
            self.clan_code,
            limit=self.PAGE_SIZE,
            offset=self.offset,
        )
        self.total_pages = max(1, math.ceil(self.total_entries / self.PAGE_SIZE))
        self.page = min(self.total_pages - 1, self.offset // self.PAGE_SIZE)
        if self.total_pages > ADAPTIVE_JUMP_THRESHOLD:
            first = discord.ui.Button(
                label=FIRST_PAGE_LABEL,
                style=discord.ButtonStyle.secondary,
                disabled=self.page == 0,
                row=0,
            )
            first.callback = self.first
            self.add_item(first)
        previous = discord.ui.Button(
            label=PREV_PAGE_LABEL,
            style=discord.ButtonStyle.secondary,
            disabled=self.page == 0,
            row=0,
        )
        following = discord.ui.Button(
            label=NEXT_PAGE_LABEL,
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= self.total_pages - 1,
            row=0,
        )
        previous.callback = self.previous
        following.callback = self.following
        self.add_item(previous)
        self.add_item(following)
        if self.total_pages > ADAPTIVE_JUMP_THRESHOLD:
            last = discord.ui.Button(
                label=LAST_PAGE_LABEL,
                style=discord.ButtonStyle.secondary,
                disabled=self.page >= self.total_pages - 1,
                row=0,
            )
            last.callback = self.last
            self.add_item(last)
        self.add_item(_back_button(self, row=0))

    def build_embed(self) -> discord.Embed:
        embed = _settings_embed(
            f"Change History - {self.clan_code}",
            "Recent changes to this clan's CWL bonus scoring setup.",
        )
        if self.total_pages > 1:
            embed.set_footer(
                text=format_page_footer(self.page + 1, self.total_pages)
            )
        if not self.entries:
            embed.add_field(name="History", value="No scoring changes have been made from Discord yet.", inline=False)
            return embed
        for entry in self.entries:
            try:
                timestamp = int(datetime.fromisoformat(str(entry.get("ts_utc"))).timestamp())
                when = f"<t:{timestamp}:R>"
            except (TypeError, ValueError):
                when = str(entry.get("ts_utc") or "-")
            actor = str(entry.get("actor_display") or "Unknown")
            summary = str(entry.get("summary") or "Scoring setup updated")
            embed.add_field(
                name=f"{when} - {actor}"[:256],
                value=summary[:1024],
                inline=False,
            )
        return embed

    async def first(self, interaction: discord.Interaction) -> None:
        view = HistoryView(self.cog, self.clan_code, offset=0)
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def previous(self, interaction: discord.Interaction) -> None:
        view = HistoryView(
            self.cog,
            self.clan_code,
            offset=max(0, self.offset - self.PAGE_SIZE),
        )
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def following(self, interaction: discord.Interaction) -> None:
        view = HistoryView(
            self.cog,
            self.clan_code,
            offset=self.offset + self.PAGE_SIZE,
        )
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

    async def last(self, interaction: discord.Interaction) -> None:
        view = HistoryView(
            self.cog,
            self.clan_code,
            offset=(self.total_pages - 1) * self.PAGE_SIZE,
        )
        self.stop()
        await edit_bound_view(interaction, embed=view.build_embed(), view=view)

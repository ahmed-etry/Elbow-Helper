"""Confirmed edits to clan health expectations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.configuration.clans import CLAN_ORDER
from elbow_helper.features.clan_health.config_labels import PLAYER_BLOCK_ORDER, PLAYER_LABELS
from elbow_helper.features.clan_health.database.config_store import ConfigValidationError
from elbow_helper.features.clan_health.ui.config_panel import (
    player_health_settings_snapshot, prepare_player_config_block,
    save_player_config_block,
)
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...engine.capability_contract import CapabilityContract
from ...access import require_evidence_access
from ...actions.contracts import ActionClass, ChangePreview, PreparedAction
from ...actions.outcomes import ActionOutcome
from ...models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ...wording import (
    ACTION_HEALTH_SETTINGS_LINE,
    ACTION_FIELD_CHANGE,
    ACTION_HEALTH_SETTINGS_LABEL,
)


def clan_health_settings_tools() -> tuple[RegisteredAgentTool, ...]:
    specs = {key: {"type": "string", "description": spec.get("help", "")}
             for block in PLAYER_BLOCK_ORDER
             for key, spec in PLAYER_LABELS[block].items() if not key.startswith("_")}
    return (RegisteredAgentTool(AgentToolDefinition(
        name="read_clan_health_settings",
        description="Read a clan's current member health expectations and field descriptions.",
        parameters={"type": "object", "properties": {
            "clan_code": {"type": "string", "enum": list(CLAN_ORDER)},
            "block": {"type": "string", "enum": PLAYER_BLOCK_ORDER},
        }, "required": ["clan_code"], "additionalProperties": False},
    ), read_health_settings,
        contract=CapabilityContract(
            entity_fields=(("clan_code", "clan"),),
            time_fields=(),
            source_scope="request_context",
            filter_fields=("block",),
        ),
            ),
        RegisteredAgentTool(AgentToolDefinition(
        name="set_clan_health_settings",
        description="Set one section of a clan's member health expectations after confirmation.",
        parameters={"type": "object", "properties": {
            "clan_code": {"type": "string", "enum": list(CLAN_ORDER)},
            "block": {"type": "string", "enum": PLAYER_BLOCK_ORDER},
            "values": {"type": "object", "properties": specs,
                       "minProperties": 1, "additionalProperties": False},
        }, "required": ["clan_code", "block", "values"], "additionalProperties": False},
    ), prepare_health_settings, AgentCapabilityEffect.COMMAND,
        ActionClass.CHANGE,
        contract=CapabilityContract(
            entity_fields=(("clan_code", "clan"),),
            time_fields=(),
            source_scope="request_context",
            filter_fields=("block", "values"),
        ),
        ),)


async def read_health_settings(context: AgentRequestContext,
                               values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    payload, _ = player_health_settings_snapshot(values["clan_code"])
    blocks = (values["block"],) if values.get("block") else PLAYER_BLOCK_ORDER
    return {"clan_code": values["clan_code"],
            "sections": [{"name": PLAYER_LABELS[block]["_title"],
                          "fields": [{"name": spec["label"], "key": key,
                                      "description": spec["help"],
                                      "value": payload[block][key],
                                      "unit": spec.get("unit", "")}
                                     for key, spec in PLAYER_LABELS[block].items()
                                     if not key.startswith("_")]}
                         for block in blocks]}


def _lines(clan: str, block: str, before: Mapping[str, Any],
           after: Mapping[str, Any]) -> tuple[str, ...]:
    lines = [ACTION_HEALTH_SETTINGS_LINE.format(clan=clan,
                                                section=PLAYER_LABELS[block]["_title"])]
    lines.extend(ACTION_FIELD_CHANGE.format(
        field=PLAYER_LABELS[block][key]["label"], old=before[key], new=value)
        for key, value in after.items() if before[key] != value)
    return tuple(lines)


async def prepare_health_settings(context: AgentRequestContext,
                                  values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    clan, block = values["clan_code"], values["block"]
    try:
        before, after, revision = prepare_player_config_block(clan, block, dict(values["values"]))
    except ConfigValidationError as exc:
        return {"status": "needs_input", "issue": "\n".join(exc.errors), "prepared_count": 0}
    except (RuntimeError, ValueError) as exc:
        return {"status": "needs_input", "issue": str(exc), "prepared_count": 0}
    if before == after:
        return {"status": "no_change"}
    lines = _lines(clan, block, before[block], after[block])

    async def recheck() -> bool:
        try:
            current, _, current_revision = prepare_player_config_block(
                clan, block, dict(values["values"]))
            return current == before and current_revision == revision
        except (RuntimeError, ValueError):
            return False

    async def run() -> ActionOutcome:
        saved = save_player_config_block(
            clan, block, dict(values["values"]), context.member,
            expected_updated_at=revision)
        return ActionOutcome("complete", "private", text=ACTION_HEALTH_SETTINGS_LABEL,
                              after={"payload": saved[block]})

    context.state.proposed_changes.append(PreparedAction(
        "set_clan_health_settings", {"clan_code": clan, "block": block},
        ChangePreview(lines, recheck, summary=ACTION_HEALTH_SETTINGS_LABEL,
                      before={"payload": before[block]}), run,
    ))
    return {"status": "confirmation_required"}


async def prepare_health_settings_undo(context: AgentRequestContext,
                                       log: Mapping[str, Any]) -> PreparedAction:
    clan, block = log["targets"]["clan_code"], log["targets"]["block"]
    expected, prior = log["after"]["payload"], log["before"]["payload"]
    try:
        current, _, revision = prepare_player_config_block(clan, block, prior)
    except (RuntimeError, ValueError) as exc:
        raise ValueError('Those Clan Health settings have changed.') from exc
    if current[block] != expected:
        raise ValueError('Those Clan Health settings have changed.')
    lines = _lines(clan, block, expected, prior)

    async def recheck() -> bool:
        live, _, live_revision = prepare_player_config_block(clan, block, prior)
        return live[block] == expected and live_revision == revision

    async def run() -> ActionOutcome:
        saved = save_player_config_block(clan, block, prior, context.member,
                                         expected_updated_at=revision)
        return ActionOutcome("complete", "private", text=ACTION_HEALTH_SETTINGS_LABEL,
                              after={"payload": saved[block]})

    return PreparedAction(
        "undo_clan_health_settings", {"clan_code": clan, "block": block},
        ChangePreview(lines, recheck, summary=ACTION_HEALTH_SETTINGS_LABEL,
                      before={"payload": expected}), run,
    )


UNDO_HANDLERS = {
    'set_clan_health_settings': prepare_health_settings_undo,
    'undo_clan_health_settings': prepare_health_settings_undo,
}

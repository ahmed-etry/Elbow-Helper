"""DB-backed clan-health config storage and audit helpers."""

from __future__ import annotations

import copy
from contextlib import closing
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from elbow_helper.configuration.clans import CLAN_ORDER

from ..config import CLAN_PROFILE_BY_CODE, DB_PATH, UTC
from ..player_health_config import (
    load_player_health_config,
    profile_health_settings,
    validate_player_profile_payload,
)

_PLAYER_CACHE: dict[str, Dict[str, Any]] = {}


class ConfigValidationError(ValueError):
    def __init__(self, errors: List[str]):
        super().__init__("Invalid config payload")
        self.errors = list(errors)


class ConfigConflictError(RuntimeError):
    pass


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def invalidate_cache(clan_code: Optional[str] = None) -> None:
    code = str(clan_code or "").upper()
    if not code:
        _PLAYER_CACHE.clear()
        return
    _PLAYER_CACHE.pop(code, None)


def _flatten_payload(payload: Dict[str, Any]) -> Dict[str, str]:
    flattened: Dict[str, str] = {}
    for block_name, block in (payload or {}).items():
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            flattened[f"{block_name}.{key}"] = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return flattened


def _actor_parts(actor: Any) -> tuple[int, str]:
    actor_id = int(getattr(actor, "id", 0) or 0)
    actor_name = (
        str(getattr(actor, "display_name", "") or "").strip()
        or str(getattr(actor, "global_name", "") or "").strip()
        or str(getattr(actor, "name", "") or "").strip()
        or str(actor_id or "unknown")
    )
    return actor_id, actor_name


def get_player_template_payload(clan_code: str) -> Dict[str, Any]:
    code = str(clan_code or "").upper()
    config, errors = load_player_health_config()
    if config is None:
        raise RuntimeError("player_health_config invalid: " + " | ".join(errors[:5]))
    template = CLAN_PROFILE_BY_CODE.get(code, "casual")
    return copy.deepcopy(profile_health_settings(config, template))


def seed_missing_configs() -> None:
    player_config, player_errors = load_player_health_config()
    if player_config is None:
        raise RuntimeError("player_health_config invalid: " + " | ".join(player_errors[:5]))
    now_iso = _utc_now_iso()
    with closing(_connect()) as conn, conn:
        cursor = conn.cursor()
        for clan_code in CLAN_ORDER:
            template = CLAN_PROFILE_BY_CODE.get(clan_code, "casual")
            cursor.execute("SELECT 1 FROM clan_player_health_config WHERE clan_code = ?", (clan_code,))
            if cursor.fetchone() is None:
                payload = profile_health_settings(player_config, template)
                cursor.execute(
                    """
                    INSERT INTO clan_player_health_config (
                        clan_code, seed_template, seeded_at_utc, updated_at_utc, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (clan_code, template, now_iso, now_iso, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
                )
        conn.commit()
    invalidate_cache()


def _load_player_config_row(clan_code: str) -> Optional[sqlite3.Row]:
    code = str(clan_code or "").upper()
    if not code:
        return None
    with closing(_connect()) as conn, conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT payload_json, updated_at_utc FROM clan_player_health_config WHERE clan_code = ?",
            (code,),
        )
        return cursor.fetchone()


def get_player_config_with_meta(clan_code: str) -> Optional[Tuple[Dict[str, Any], str]]:
    code = str(clan_code or "").upper()
    row = _load_player_config_row(code)
    if row is None:
        return None
    template = CLAN_PROFILE_BY_CODE.get(code, "casual")
    payload = profile_health_settings({"profiles": {template: json.loads(str(row["payload_json"] or "{}"))}}, template)
    _PLAYER_CACHE[code] = payload
    return copy.deepcopy(payload), str(row["updated_at_utc"] or "")


def get_player_config(clan_code: str) -> Optional[Dict[str, Any]]:
    code = str(clan_code or "").upper()
    if not code:
        return None
    cached = _PLAYER_CACHE.get(code)
    if cached is not None:
        return copy.deepcopy(cached)
    result = get_player_config_with_meta(code)
    if result is None:
        return None
    payload, _ = result
    return payload


def save_player_config(
    clan_code: str,
    new_payload: Dict[str, Any],
    actor: Any,
    expected_updated_at: Optional[str] = None,
) -> int:
    code = str(clan_code or "").upper()
    template = CLAN_PROFILE_BY_CODE.get(code, "casual")
    errors = validate_player_profile_payload(new_payload, template)
    fallback_payload = get_player_template_payload(code)
    normalized_payload = profile_health_settings({"profiles": {template: new_payload}}, template)
    if errors:
        raise ConfigValidationError(errors)

    now_iso = _utc_now_iso()
    actor_id, actor_name = _actor_parts(actor)
    with closing(_connect()) as conn, conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT payload_json, updated_at_utc FROM clan_player_health_config WHERE clan_code = ?",
            (code,),
        )
        row = cursor.fetchone()
        exists = row is not None
        current_updated_at = str(row["updated_at_utc"] or "") if row else None
        if expected_updated_at is not None and current_updated_at != expected_updated_at:
            raise ConfigConflictError("These settings changed after you opened the panel. Reload it and try again.")
        if row is not None:
            old_payload = profile_health_settings(
                {"profiles": {template: json.loads(str(row["payload_json"] or "{}"))}},
                template,
            )
        else:
            old_payload = fallback_payload
        old_flat = _flatten_payload(old_payload)
        new_flat = _flatten_payload(normalized_payload)
        changed_keys = [key for key, new_value in new_flat.items() if old_flat.get(key) != new_value]
        payload_json = json.dumps(normalized_payload, ensure_ascii=False, sort_keys=True)
        if exists:
            cursor.execute(
                "UPDATE clan_player_health_config SET payload_json = ?, updated_at_utc = ? WHERE clan_code = ?",
                (payload_json, now_iso, code),
            )
        else:
            cursor.execute(
                """
                INSERT INTO clan_player_health_config (
                    clan_code, seed_template, seeded_at_utc, updated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (code, template, now_iso, now_iso, payload_json),
            )
        for changed_key in changed_keys:
            block, key = changed_key.split(".", 1)
            cursor.execute(
                """
                INSERT INTO clan_health_config_audit (
                    ts_utc, clan_code, layer, block, key, old_value, new_value, actor_discord_id, actor_display
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_iso,
                    code,
                    "player",
                    block,
                    key,
                    old_flat.get(changed_key),
                    new_flat.get(changed_key),
                    actor_id,
                    actor_name,
                ),
            )
        conn.commit()
    invalidate_cache(code)
    return len(changed_keys)

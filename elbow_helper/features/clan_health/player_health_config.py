"""Config loader and resolver for player-health expectations."""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from elbow_helper.infrastructure.persistence import read_json

from .config import CLAN_PROFILE_BY_CODE, PROFILE_NAMES, PROFILE_NAMES_ORDERED

LOGGER = logging.getLogger(__name__)
PLAYER_HEALTH_CONFIG_FILE = Path("data/clan_health/player_health_config.json")

_CACHE_DATA: Optional[Dict[str, Any]] = None
_CACHE_MTIME: Optional[float] = None
_CACHE_ERRORS: List[str] = []
_DEFAULT_MTIME = -1.0

PLAYER_BLOCK_ORDER: tuple[str, ...] = ("war", "raids", "clan_games")

PLAYER_BLOCK_FIELDS: Dict[str, tuple[str, ...]] = {
    "war": ("wars_to_join", "missed_attack_rate_percent"),
    "raids": ("minimum_capital_gold_per_event",),
    "clan_games": ("minimum_points_per_event",),
}

PROFILE_GRADE_BUCKETS: Dict[str, Dict[str, int]] = {
    "competitive": {"war": 80, "raids": 10, "clan_games": 10},
    "casual": {"cwl": 50, "raids": 20, "clan_games": 30},
    "starter": {"cwl": 50, "raids": 20, "clan_games": 30},
    "utility": {},
}

PROFILE_RAID_SERIOUS_MISS_30D: Dict[str, int] = {
    "competitive": 3,
    "casual": 3,
    "starter": 3,
    "utility": 0,
}


def _default_player_profile(profile_name: str) -> Dict[str, Any]:
    profile = str(profile_name or "casual").strip().lower()
    defaults: Dict[str, Dict[str, Any]] = {
        "competitive": {
            "war": {"wars_to_join": 3, "missed_attack_rate_percent": 10},
            "raids": {"minimum_capital_gold_per_event": 0},
            "clan_games": {"minimum_points_per_event": 4_000},
        },
        "casual": {
            "war": {"wars_to_join": 0, "missed_attack_rate_percent": 10},
            "raids": {"minimum_capital_gold_per_event": 0},
            "clan_games": {"minimum_points_per_event": 1_000},
        },
        "starter": {
            "war": {"wars_to_join": 0, "missed_attack_rate_percent": 10},
            "raids": {"minimum_capital_gold_per_event": 0},
            "clan_games": {"minimum_points_per_event": 1_000},
        },
        "utility": {
            "war": {"wars_to_join": 0, "missed_attack_rate_percent": 0},
            "raids": {"minimum_capital_gold_per_event": 0},
            "clan_games": {"minimum_points_per_event": 0},
        },
    }
    return copy.deepcopy(defaults.get(profile, defaults["casual"]))


def _default_player_health_config() -> Dict[str, Any]:
    return {"profiles": {name: _default_player_profile(name) for name in PROFILE_NAMES_ORDERED}}


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def profile_grading_model(profile_name: str) -> Dict[str, Any]:
    profile = str(profile_name or "casual").strip().lower()
    return {
        "profile_name": profile,
        "bucket_weights": copy.deepcopy(PROFILE_GRADE_BUCKETS.get(profile, PROFILE_GRADE_BUCKETS["casual"])),
        "raid_serious_miss_30d": int(PROFILE_RAID_SERIOUS_MISS_30D.get(profile, 3)),
        "regular_wars_are_context_only": profile in {"casual", "starter", "utility"},
        "track_utility_grading": profile != "utility",
    }


def _canonical_profile_payload(profile_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    canonical = _default_player_profile(profile_name)
    source = payload if isinstance(payload, dict) else {}

    for block_name, field_names in PLAYER_BLOCK_FIELDS.items():
        block = source.get(block_name)
        if not isinstance(block, dict):
            continue
        for field_name in field_names:
            if block.get(field_name) is not None:
                canonical[block_name][field_name] = block[field_name]
    return canonical


def _normalize_profile(profile_name: str, payload: Any) -> Dict[str, Any]:
    canonical = _canonical_profile_payload(profile_name, payload if isinstance(payload, dict) else {})
    canonical["war"]["wars_to_join"] = max(0, _coerce_int(canonical["war"].get("wars_to_join"), 0))
    canonical["war"]["missed_attack_rate_percent"] = min(
        100,
        max(0, _coerce_int(canonical["war"].get("missed_attack_rate_percent"), 0)),
    )
    canonical["raids"]["minimum_capital_gold_per_event"] = max(
        0,
        _coerce_int(canonical["raids"].get("minimum_capital_gold_per_event"), 0),
    )
    canonical["clan_games"]["minimum_points_per_event"] = max(
        0,
        _coerce_int(canonical["clan_games"].get("minimum_points_per_event"), 0),
    )
    return canonical


def _normalize_player_health_config(config: Dict[str, Any]) -> Dict[str, Any]:
    profiles = config.get("profiles")
    source_profiles = profiles if isinstance(profiles, dict) else {}
    return {
        "profiles": {
            profile_name: _normalize_profile(profile_name, source_profiles.get(profile_name))
            for profile_name in PROFILE_NAMES_ORDERED
        }
    }


def _validate_profile(profile_name: str, payload: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(payload, dict):
        return [f"profile '{profile_name}' must be an object."]
    for block_name in PLAYER_BLOCK_ORDER:
        block = payload.get(block_name)
        if not isinstance(block, dict):
            errors.append(f"profile '{profile_name}' missing block '{block_name}'.")
            continue
        for key in PLAYER_BLOCK_FIELDS[block_name]:
            value = block.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"profile '{profile_name}' {block_name}.{key} must be numeric.")
                continue
            if float(value) < 0:
                errors.append(f"profile '{profile_name}' {block_name}.{key} must be non-negative.")
    return errors


def validate_player_profile_payload(payload: Dict[str, Any], profile_name: str = "custom") -> List[str]:
    return _validate_profile(profile_name, _normalize_profile(profile_name, payload))


def _validate_player_health_config(config: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not isinstance(config, dict):
        return ["Config root must be a JSON object."]
    profiles = config.get("profiles")
    if not isinstance(profiles, dict):
        return ["profiles must be an object."]
    for required_profile in PROFILE_NAMES:
        if required_profile not in profiles:
            errors.append(f"profiles missing '{required_profile}'")
    normalized = _normalize_player_health_config(config)
    for profile_name in PROFILE_NAMES_ORDERED:
        errors.extend(_validate_profile(profile_name, normalized["profiles"].get(profile_name)))
    return errors


def load_player_health_config() -> Tuple[Optional[Dict[str, Any]], List[str]]:
    global _CACHE_DATA, _CACHE_ERRORS, _CACHE_MTIME
    try:
        mtime = PLAYER_HEALTH_CONFIG_FILE.stat().st_mtime
    except FileNotFoundError:
        if _CACHE_DATA is not None and _CACHE_MTIME == _DEFAULT_MTIME:
            return copy.deepcopy(_CACHE_DATA), list(_CACHE_ERRORS)
        _CACHE_DATA = _default_player_health_config()
        _CACHE_ERRORS = []
        _CACHE_MTIME = _DEFAULT_MTIME
        LOGGER.warning("Player health config file missing; using built-in defaults")
        return copy.deepcopy(_CACHE_DATA), []
    except OSError as exc:
        return None, [f"Failed reading config file metadata: {exc}"]

    if _CACHE_DATA is not None and _CACHE_MTIME == mtime:
        return copy.deepcopy(_CACHE_DATA), list(_CACHE_ERRORS)

    try:
        raw_config = read_json(PLAYER_HEALTH_CONFIG_FILE)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        _CACHE_DATA = None
        _CACHE_ERRORS = [f"Failed reading config file: {exc}"]
        _CACHE_MTIME = mtime
        return None, list(_CACHE_ERRORS)

    config = _normalize_player_health_config(raw_config)
    errors = _validate_player_health_config(config)
    _CACHE_DATA = config if not errors else None
    _CACHE_ERRORS = errors
    _CACHE_MTIME = mtime
    return copy.deepcopy(_CACHE_DATA), list(_CACHE_ERRORS)


def profile_health_settings(config: Dict[str, Any], profile_name: str) -> Dict[str, Any]:
    profiles = config.get("profiles", {})
    if not isinstance(profiles, dict):
        return _default_player_profile("casual")
    payload = profiles.get(profile_name)
    if isinstance(payload, dict):
        return _normalize_profile(profile_name, payload)
    fallback = profiles.get("casual")
    return _normalize_profile("casual", fallback)


def _resolved_rules(
    *,
    profile_name: str,
    family_config: Optional[Dict[str, Any]],
    clan_payload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    built_in = _default_player_profile(profile_name)
    family_profile = profile_health_settings(family_config or _default_player_health_config(), profile_name)
    clan_profile = _normalize_profile(profile_name, clan_payload or {})
    use_built_in = family_config is None

    resolved = _default_player_profile(profile_name)
    for block_name in PLAYER_BLOCK_ORDER:
        for key in PLAYER_BLOCK_FIELDS[block_name]:
            family_value = family_profile[block_name][key]
            clan_value = clan_profile[block_name][key]
            if clan_payload is not None and clan_value != family_value:
                value = clan_value
            elif use_built_in:
                value = built_in[block_name][key]
            else:
                value = family_value
            resolved[block_name][key] = value

    resolved["_profile_name"] = profile_name
    resolved["_grading_model"] = profile_grading_model(profile_name)
    return resolved


def effective_player_rules(clan_code: str) -> Dict[str, Any]:
    from .database.config_store import get_player_config

    code = str(clan_code or "").upper()
    profile_name = CLAN_PROFILE_BY_CODE.get(code, "casual")
    family_config, errors = load_player_health_config()
    if family_config is None:
        LOGGER.warning("player_health_config invalid; using built-in defaults: %s", " | ".join(errors[:5]))
    clan_payload = get_player_config(code)
    return _resolved_rules(
        profile_name=profile_name,
        family_config=family_config,
        clan_payload=clan_payload,
    )


def raid_scoring_enabled(profile_rules: Dict[str, Any]) -> bool:
    if not isinstance(profile_rules, dict):
        return True
    return str(profile_rules.get("_profile_name") or "").strip().lower() != "utility"

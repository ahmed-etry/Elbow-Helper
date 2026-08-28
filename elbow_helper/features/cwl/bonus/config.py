"""CWL bonus configuration, audit, and retention helpers."""

from __future__ import annotations

import copy
import json
import logging
import math
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple

from ..config import BONUS_CONFIG_FILE
from ..config import CWL_CLAN_CODES
from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic

LOGGER = logging.getLogger(__name__)

BONUS_CONFIG_BACKUP_FILE = BONUS_CONFIG_FILE.with_name("cwl_bonus_config.backup.json")
BONUS_CONFIG_AUDIT_FILE = BONUS_CONFIG_FILE.with_name("cwl_bonus_config_audit.json")
BONUS_CONFIG_AUDIT_LIMIT = 500


class BonusConfigConflictError(RuntimeError):
    """Raised when a panel tries to save an outdated config snapshot."""


class BonusConfigValidationError(ValueError):
    def __init__(self, errors: List[str]):
        super().__init__(" | ".join(errors))
        self.errors = errors


class BonusConfigRepository:
    """Own validated CWL bonus configuration and its audit history."""

    @staticmethod
    def _default_bonus_clan_config() -> Dict[str, Any]:
        attacker_th_levels = list(range(8, 19))
        defender_th_levels = list(range(6, 19))
        max_downhit = 2
        max_uphit = 8
        matchup_expected = {
            f"{attacker_th}:{defender_th}": None
            for attacker_th in attacker_th_levels
            for defender_th in defender_th_levels
            if (attacker_th - defender_th) <= max_downhit
            and (defender_th - attacker_th) <= max_uphit
        }
        return {
            "attacker_th_levels": attacker_th_levels,
            "defender_th_levels": defender_th_levels,
            "max_downhit": max_downhit,
            "max_uphit": max_uphit,
            "downhit_penalty_per_level": 0.15,
            "uphit_bonus_per_level": 0.10,
            "downhit_severe_after": 0,
            "downhit_severe_base": 0.20,
            "downhit_severe_multiplier": 2.0,
            "matchup_expected": matchup_expected,
        }

    def _default_bonus_config(self) -> Dict[str, Any]:
        return {
            "version": 2,
            "revision": 0,
            "clans": {
                clan_code: self._default_bonus_clan_config()
                for clan_code in CWL_CLAN_CODES
            },
            "clan_meta": {
                clan_code: {
                    "updated_at_utc": None,
                    "updated_by_id": None,
                    "updated_by_name": None,
                }
                for clan_code in CWL_CLAN_CODES
            },
        }

    def ensure(self) -> None:
        if BONUS_CONFIG_FILE.exists():
            return
        try:
            write_json_atomic(
                BONUS_CONFIG_FILE,
                self._default_bonus_config(),
                indent=2,
                ensure_ascii=False,
            )
            LOGGER.info("Created default config at %s", BONUS_CONFIG_FILE)
        except (OSError, TypeError):
            LOGGER.exception("Failed creating config file at %s", BONUS_CONFIG_FILE)

    @staticmethod
    def _bonus_matchup_keys(
        attacker_th_levels: List[int],
        defender_th_levels: List[int],
        max_downhit: int,
        max_uphit: int,
    ) -> Set[str]:
        return {
            f"{attacker_th}:{defender_th}"
            for attacker_th in attacker_th_levels
            for defender_th in defender_th_levels
            if (attacker_th - defender_th) <= max_downhit
            and (defender_th - attacker_th) <= max_uphit
        }

    def _validate_bonus_clan_config(self, clan_code: str, payload: Any) -> List[str]:
        errors: List[str] = []
        if not isinstance(payload, dict):
            return [f"{clan_code} scoring settings couldn't be read."]

        attacker_levels_raw = payload.get("attacker_th_levels")
        defender_levels_raw = payload.get("defender_th_levels")
        if not isinstance(attacker_levels_raw, list) or not attacker_levels_raw:
            return [f"{clan_code} requires attacker TH levels."]
        if not isinstance(defender_levels_raw, list) or not defender_levels_raw:
            return [f"{clan_code} requires defender TH levels."]
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in attacker_levels_raw):
            errors.append(f"{clan_code} attacker TH levels must be whole numbers.")
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in defender_levels_raw):
            errors.append(f"{clan_code} defender TH levels must be whole numbers.")
        if errors:
            return errors

        max_downhit = payload.get("max_downhit")
        max_uphit = payload.get("max_uphit")
        if not isinstance(max_downhit, int) or isinstance(max_downhit, bool) or max_downhit < 0:
            errors.append(f"{clan_code} maximum downhit must be a non-negative whole number.")
        if not isinstance(max_uphit, int) or isinstance(max_uphit, bool) or max_uphit < 0:
            errors.append(f"{clan_code} maximum uphit must be a non-negative whole number.")

        severe_after = payload.get("downhit_severe_after")
        if not isinstance(severe_after, int) or isinstance(severe_after, bool) or severe_after < 0:
            errors.append(f"{clan_code} extra downhit threshold must be a non-negative whole number.")

        for field_name, label in (
            ("downhit_penalty_per_level", "downhit penalty"),
            ("uphit_bonus_per_level", "uphit credit"),
            ("downhit_severe_base", "extra downhit base"),
        ):
            value = payload.get(field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                errors.append(f"{clan_code} {label} must be a non-negative number.")

        multiplier = payload.get("downhit_severe_multiplier")
        if (
            isinstance(multiplier, bool)
            or not isinstance(multiplier, (int, float))
            or not math.isfinite(float(multiplier))
            or multiplier < 1
        ):
            errors.append(f"{clan_code} downhit growth multiplier must be at least 1.")

        if errors:
            return errors

        attacker_levels = sorted(set(attacker_levels_raw))
        defender_levels = sorted(set(defender_levels_raw))
        expected_keys = self._bonus_matchup_keys(
            attacker_levels,
            defender_levels,
            max_downhit,
            max_uphit,
        )
        matchup_expected = payload.get("matchup_expected")
        if not isinstance(matchup_expected, dict):
            return [f"{clan_code} Expected Scores couldn't be read."]

        missing_keys = sorted(expected_keys - set(matchup_expected.keys()))
        if missing_keys:
            preview = ", ".join(missing_keys[:8])
            suffix = " ..." if len(missing_keys) > 8 else ""
            errors.append(f"{clan_code} is missing Expected Scores for {preview}{suffix}.")

        invalid_values: List[str] = []
        impossible_values: List[str] = []
        for key in expected_keys:
            value = matchup_expected.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0 <= value <= 3
            ):
                invalid_values.append(key)
            elif 0.5 <= float(value) < 1.0:
                impossible_values.append(key)
        if invalid_values:
            preview = ", ".join(invalid_values[:8])
            suffix = " ..." if len(invalid_values) > 8 else ""
            errors.append(f"{clan_code} has invalid Expected Scores at {preview}{suffix}.")
        if impossible_values:
            preview = ", ".join(impossible_values[:8])
            suffix = " ..." if len(impossible_values) > 8 else ""
            errors.append(
                f"{clan_code} has impossible Expected Scores from 0.50 to 0.99 at {preview}{suffix}."
            )
        return errors

    def _validate_bonus_config(self, config: Dict[str, Any]) -> List[str]:
        if not isinstance(config, dict):
            return ["CWL bonus settings couldn't be read."]
        clans = config.get("clans")
        if not isinstance(clans, dict):
            return ["CWL bonus settings don't include any clan scoring setups."]

        errors: List[str] = []
        missing_clans = sorted(set(CWL_CLAN_CODES) - set(clans.keys()))
        if missing_clans:
            errors.append(f"CWL bonus settings are missing clans: {', '.join(missing_clans)}.")
        for clan_code in CWL_CLAN_CODES:
            if clan_code in clans:
                errors.extend(self._validate_bonus_clan_config(clan_code, clans[clan_code]))
        return errors

    def load(self) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        self.ensure()
        try:
            raw_config = read_json(BONUS_CONFIG_FILE)
        except FileNotFoundError:
            LOGGER.error(
                "CWL bonus config file missing after initialization: %s",
                BONUS_CONFIG_FILE,
            )
            return None, ["CWL bonus settings aren't available."]
        except (OSError, json.JSONDecodeError, TypeError):
            LOGGER.exception("Failed reading CWL bonus config: %s", BONUS_CONFIG_FILE)
            return None, ["CWL bonus settings aren't available."]

        errors = self._validate_bonus_config(raw_config)
        if errors:
            return None, errors
        return raw_config, []

    @staticmethod
    def _bonus_actor(actor: Any) -> Tuple[Optional[int], str]:
        actor_id = getattr(actor, "id", None)
        actor_name = str(
            getattr(actor, "display_name", None)
            or getattr(actor, "name", None)
            or "Unknown"
        )
        return int(actor_id) if actor_id is not None else None, actor_name

    @staticmethod
    def revision(config: Dict[str, Any]) -> int:
        return int(config.get("revision") or 0)

    def _write_bonus_audit_entry(self, entry: Dict[str, Any]) -> None:
        payload: Dict[str, Any] = {"entries": []}
        try:
            if BONUS_CONFIG_AUDIT_FILE.exists():
                loaded = read_json(BONUS_CONFIG_AUDIT_FILE)
                if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
                    payload = loaded
        except (OSError, json.JSONDecodeError, TypeError):
            LOGGER.warning("Could not read CWL bonus config audit; starting a new audit file")
        payload["entries"] = [entry, *payload["entries"]][:BONUS_CONFIG_AUDIT_LIMIT]
        write_json_atomic(
            BONUS_CONFIG_AUDIT_FILE,
            payload,
            indent=2,
            ensure_ascii=False,
        )

    def save_clan(
        self,
        clan_code: str,
        new_payload: Dict[str, Any],
        actor: Any,
        *,
        expected_revision: int,
        summary: str,
    ) -> Dict[str, Any]:
        code = str(clan_code or "").upper()
        config, errors = self.load()
        if config is None:
            raise BonusConfigValidationError(errors)
        if self.revision(config) != int(expected_revision):
            raise BonusConfigConflictError(
                "These settings changed while this panel was open. Nothing was saved. Reload the section and try again."
            )

        validation_errors = self._validate_bonus_clan_config(code, new_payload)
        if validation_errors:
            raise BonusConfigValidationError(validation_errors)

        updated = copy.deepcopy(config)
        updated["clans"][code] = copy.deepcopy(new_payload)
        updated["revision"] = self.revision(config) + 1
        now_iso = datetime.now(dt_timezone.utc).isoformat()
        actor_id, actor_name = self._bonus_actor(actor)
        updated.setdefault("clan_meta", {})[code] = {
            "updated_at_utc": now_iso,
            "updated_by_id": actor_id,
            "updated_by_name": actor_name,
        }
        full_errors = self._validate_bonus_config(updated)
        if full_errors:
            raise BonusConfigValidationError(full_errors)

        try:
            write_json_atomic(
                BONUS_CONFIG_BACKUP_FILE,
                config,
                indent=2,
                ensure_ascii=False,
            )
            write_json_atomic(
                BONUS_CONFIG_FILE,
                updated,
                indent=2,
                ensure_ascii=False,
            )
        except (OSError, TypeError) as exc:
            raise RuntimeError("Couldn't save the scoring setup. Nothing was changed.") from exc

        try:
            self._write_bonus_audit_entry(
                {
                    "ts_utc": now_iso,
                    "clan_code": code,
                    "summary": str(summary),
                    "actor_discord_id": actor_id,
                    "actor_display": actor_name,
                    "revision": updated["revision"],
                }
            )
        except (OSError, TypeError):
            LOGGER.exception("Saved CWL bonus config but failed writing its audit entry")
        return updated

    def copy_clan(
        self,
        source_clan: str,
        target_clan: str,
        actor: Any,
        *,
        expected_revision: int,
    ) -> Dict[str, Any]:
        config, errors = self.load()
        if config is None:
            raise BonusConfigValidationError(errors)
        source = str(source_clan or "").upper()
        target = str(target_clan or "").upper()
        source_payload = (config.get("clans") or {}).get(source)
        if not isinstance(source_payload, dict):
            raise BonusConfigValidationError([f"CWL bonus scoring settings for {source} aren't available. Check that clan's settings and try again."])
        return self.save_clan(
            target,
            copy.deepcopy(source_payload),
            actor,
            expected_revision=expected_revision,
            summary=f"Copied scoring setup from {source}",
        )

    @staticmethod
    def history(
        clan_code: str,
        *,
        limit: int = 8,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        try:
            payload = read_json(BONUS_CONFIG_AUDIT_FILE)
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
            return [], 0
        entries = payload.get("entries") if isinstance(payload, dict) else []
        if not isinstance(entries, list):
            return [], 0
        matching = [
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("clan_code") == str(clan_code).upper()
        ]
        start = max(0, int(offset))
        page_size = max(1, int(limit))
        page = matching[start : start + page_size]
        return page, len(matching)

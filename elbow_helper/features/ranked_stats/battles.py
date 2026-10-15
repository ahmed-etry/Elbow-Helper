"""Ranked battle identities and polling based on the returned rolling log."""

from datetime import datetime, timezone
import hashlib
import json
import logging

from elbow_helper.domain.player_tags import normalize_player_tag


INITIAL_POLL_SECONDS = 300
MIN_POLL_SECONDS = 300
MAX_POLL_SECONDS = 3600
RANKED_MODES = frozenset({"RANKED", "LEGEND"})
LOGGER = logging.getLogger(__name__)
_seen_modes = set()


def report_unknown_mode(mode):
    # Logged once per mode so a renamed ranked mode is visible without flooding the log.
    if mode != "HOME_VILLAGE" and mode not in _seen_modes:
        _seen_modes.add(mode)
        LOGGER.info("Ranked battle log treats battle type %s as unranked", mode)


def battle_time(value):
    if not isinstance(value, str):
        raise ValueError("Missing battleTimestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Battle timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def opponent_identity(item):
    tag = normalize_player_tag(item.get("opponentPlayerTag"))
    name = item.get("opponentName")
    return tag or (name if isinstance(name, str) and name else None)


def log_identities(payload):
    """Compare all log modes using stable fields, including farming battles."""
    items = payload.get("items") if isinstance(payload, dict) else payload
    identities = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            timestamp = battle_time(item.get("battleTimestamp")).isoformat()
        except ValueError:
            continue
        identities.add(json.dumps([
            timestamp, item.get("battleType"), item.get("attack"),
            opponent_identity(item),
        ]))
    return identities


def parse_log(payload, observation, report_missing):
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("Missing battle log entries")
    timestamps, records, skipped = [], {}, 0
    for item in items:
        if not isinstance(item, dict):
            report_missing("battlelog.entry", True)
            skipped += 1
            continue
        try:
            when = battle_time(item.get("battleTimestamp"))
            timestamps.append(when)
            report_missing("battlelog.battleTimestamp", False)
        except ValueError:
            report_missing("battlelog.battleTimestamp", True)
            when = None
        mode = item.get("battleType")
        if not isinstance(mode, str) or not mode:
            report_missing("battlelog.battleType", True)
            skipped += 1
            continue
        report_missing("battlelog.battleType", False)
        mode = mode.upper()
        if mode not in RANKED_MODES:
            # Farming, friendly and other modes are not ranked battles.
            report_unknown_mode(mode)
            continue
        attack = item.get("attack")
        opponent = item.get("opponentPlayerTag")
        opponent = normalize_player_tag(opponent)
        report_missing("battlelog.attack", type(attack) is not bool)
        if when is None or type(attack) is not bool:
            skipped += 1
            continue
        timestamp = when.isoformat(timespec="microseconds")
        # Prefer the opponent tag; use the name only when the tag is absent.
        # Results, loot and army data can be corrected in later polls.
        identity = json.dumps([observation["player_tag"], timestamp, mode, attack, opponent_identity(item)])
        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        records[key] = {
            "battle_key": key, "player_tag": observation["player_tag"],
            "battle_at": timestamp, "battle_type": mode, "attack": int(attack),
            "opponent_tag": opponent, "member_id": observation["member_id"],
            "clan_tag": observation["clan_tag"], "player_name": observation["player_name"],
            "observed_at": observation["observed_at"], "payload": item,
        }
    count = len(items)
    oldest, newest = (min(timestamps), max(timestamps)) if timestamps else (None, None)
    # No fixed capacity is promised in the inspected documentation. Estimate
    # turnover from N returned records and their average spacing, and sample
    # at a quarter of that window. Include farming and defenses in the rate.
    interval = INITIAL_POLL_SECONDS
    if count and (len(timestamps) != count or count == 1):
        interval = MIN_POLL_SECONDS
    elif count > 1:
        span = (newest - oldest).total_seconds()
        interval = max(MIN_POLL_SECONDS, min(MAX_POLL_SECONDS, span * count / (count - 1) / 4))
    poll = {**{field: observation[field] for field in ("player_tag", "observed_at", "member_id", "clan_tag")},
            "status": "partial" if skipped else "ok", "total_entries": count,
            "oldest_battle_at": oldest.isoformat(timespec="microseconds") if oldest else None,
            "newest_battle_at": newest.isoformat(timespec="microseconds") if newest else None,
            "skipped_entries": skipped, "poll_interval_seconds": interval}
    return poll, list(records.values())

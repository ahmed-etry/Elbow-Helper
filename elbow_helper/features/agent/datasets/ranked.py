"""Agent access to stored ranked observations, matching live Clash access."""

import asyncio
from datetime import datetime, timedelta, timezone
import sqlite3

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.ai import AgentToolDefinition
from ..access import require_evidence_access
from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool
from ..research.history import search_date
from .clash import read_clash


def history_coverage(result, after, before):
    accounts = result.get("accounts", [])
    reasons = []
    if not accounts or not any(row.get("battle_records", 0) for row in accounts):
        reasons.append("empty_ranked_history")
    if any(row.get("counts_complete") is False for row in accounts):
        reasons.append("incomplete_ranked_counts")
    if any(row.get("failed_battle_log_polls", 0) for row in accounts):
        reasons.append("failed_battle_log_polls")
    if result.get("coverage_complete") is False or result.get("complete") is False:
        reasons.append("incomplete_ranked_history")
    if result.get("unobserved_selections"):
        reasons.append("unobserved_selections")
    if result.get("next_offset") is not None:
        reasons.append("more_accounts_available")
    try:
        returned_after = search_date(result.get("after"))
        returned_before = search_date(result.get("before"))
        if ((returned_after is not None and returned_after > after)
                or (returned_before is not None and returned_before < before)):
            reasons.append("shorter_observation_window")
    except (ValueError, TypeError):
        reasons.append("shorter_observation_window")
    for row in accounts:
        try:
            first = search_date(row.get("first_observed_at"))
            last = search_date(row.get("last_observed_at"))
            if first is None or last is None or first > after or last < before:
                reasons.append("shorter_observation_window")
                break
        except (ValueError, TypeError):
            reasons.append("shorter_observation_window")
            break
    return list(dict.fromkeys(reasons))


async def _live_batch(context, kind, tags):
    items = []
    observed_at = None
    for start in range(0, len(tags), 20):
        result = await read_clash(context, {"kind": kind, "tags": tags[start:start + 20], "limit": 50})
        if "error" in result:
            return result
        items.extend(result["items"])
        observed_at = result["observed_at"]
    return {"items": items, "observed_at": observed_at}


async def _live_history(context, selector, values, accounts):
    # Current rolling logs do not replace historical counts or ownership.
    tags = {row["player_tag"] for row in accounts}
    if selector == "account_tags":
        tags.update(values)
    elif selector == "member_ids" and context.account_links is not None:
        links = await asyncio.to_thread(context.account_links.get_links_for_members, values)
        tags.update(row["player_tag"] for rows in links.values() for row in rows)
    tags = sorted({tag for value in tags if (tag := normalize_player_tag(value)) is not None})
    if not tags:
        return {"error": "No accounts are available for a live battle log read."}
    result = await _live_batch(context, "battle_log", tags)
    return result


async def read_ranked_stats(context, arguments):
    await require_evidence_access(context)
    if context.ranked_queries is None:
        return {"error": "Ranked observations are not available."}
    selectors = [key for key in ("member_ids", "account_tags", "clan_tags") if arguments.get(key)]
    if len(selectors) != 1:
        return {"error": "Choose members, accounts or clans."}
    selector = selectors[0]
    values = arguments[selector]
    if not isinstance(values, list) or not 1 <= len(values) <= 200:
        return {"error": "Choose between one and two hundred members, accounts or clans."}
    if selector == "member_ids":
        if any(type(value) is not int or value <= 0 for value in values):
            return {"error": "Supply valid member IDs."}
    else:
        normalized = []
        for value in values:
            clan = CLANS.get(value.upper()) if isinstance(value, str) and selector == "clan_tags" else None
            tag = normalize_player_tag(clan.tag if clan else value)
            if tag is None:
                return {"error": "Use valid Clash tags or family clan codes."}
            normalized.append(tag)
        values = normalized
    try:
        after = search_date(arguments.get("after"))
        before = search_date(arguments.get("before")) or datetime.now(timezone.utc)
        if after is None or after >= before or (before - after) > timedelta(days=90):
            raise ValueError("Invalid window")
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 25)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 25:
            raise ValueError("Invalid page")
    except (ValueError, TypeError):
        return {"error": "Use ISO dates for a window of up to ninety days and a valid page."}
    try:
        result = await asyncio.to_thread(context.ranked_queries.window,
            after=after.isoformat(timespec="microseconds"), before=before.isoformat(timespec="microseconds"), selector=selector,
            values=tuple(dict.fromkeys(values)), offset=offset, limit=limit)
    except (sqlite3.Error, ValueError):
        return {"error": "Ranked observations could not be read."}
    await require_evidence_access(context)
    reasons = history_coverage(result, after, before)
    if reasons:
        result = {**result, "complete": False,
                  "history_coverage": {
                      "status": "empty" if "empty_ranked_history" in reasons else "partial",
                      "message": ("Stored ranked history is empty for this selection."
                                  if "empty_ranked_history" in reasons else
                                  "Stored ranked history is incomplete for the requested window."),
                      "requested_after": after.isoformat(), "requested_before": before.isoformat(),
                      "reasons": reasons,
                  },
                  "flags": {"status": "partial", "truncated": False, "limits": reasons}}
        try:
            # A whole clan would fan out to every member's log; those stay explicit live reads.
            live = ({"skipped": "Live battle logs are read automatically only for specific accounts or members."}
                    if selector == "clan_tags" else
                    await _live_history(context, selector, values, result["accounts"]))
        except sqlite3.Error:
            live = {"error": "Accounts could not be read for a live battle log read."}
        result["live_battle_logs"] = {
            "scope": "latest_rolling_log", "covers_requested_window": False,
            "result": live,
        }
        await require_evidence_access(context)
    return result


def ranked_tools():
    return (RegisteredAgentTool(
        AgentToolDefinition(
            "read_ranked_stats", "Read ranked battle counts and snapshots for members, accounts or clans.",
            {"type": "object", "properties": {
                "member_ids": {"type": "array", "minItems": 1, "maxItems": 200,
                               "items": {"type": "integer", "minimum": 1}},
                "account_tags": {"type": "array", "minItems": 1, "maxItems": 200,
                                 "items": {"type": "string"}},
                "clan_tags": {"type": "array", "minItems": 1, "maxItems": 200,
                              "items": {"type": "string"}},
                "after": {"type": "string", "maxLength": 40},
                "before": {"type": "string", "maxLength": 40},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            }, "required": ["after"], "additionalProperties": False},
            usage_note="Choose one selector; max 90 days. Attacks count stored ranked/Legend records, including losses; check coverage. Quotas stay null; dates are UTC.",
        ), read_ranked_stats,
        contract=CapabilityContract(entity_fields=(("member_ids", "discord_member_set"),
            ("account_tags", "clash_account_set"), ("clan_tags", "clan_set"))),
    ),)

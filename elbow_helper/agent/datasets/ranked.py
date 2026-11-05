"""Agent access to the family record's ranked battles; gaps are reported, never filled live."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.ai import AgentToolDefinition
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigrationError
from ..access import require_evidence_access
from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool
from ..research.history import search_date
from ..capabilities.validation import invalid_arguments
from ..read_cache import source_revision
from ..plan.input_rules import RULES

SELECTORS = ["member_ids", "account_tags", "clan_tags"]
SECOND = 1_000_000
FAILURES = (OSError, sqlite3.Error, TimeoutError, SQLiteMigrationError, ValueError)
LIMITATIONS = ["selection_rank_uses_last_stored_trophies_within_selected_accounts",
               "attacks_used_counts_stored_ranked_and_legend_attacks_including_losses",
               "log_coverage_gaps_can_hide_battles", "attack_quota_unavailable",
               "daily_counts_use_battle_timestamp_UTC",
               "owner_is_current_bot_link_and_clan_is_membership_at_battle_time"]


@dataclass(frozen=True)
class DailyBattles:
    date: str
    attacks_used: int
    defenses: int


@dataclass(frozen=True)
class LogGap:
    after: str
    before: str
    cause: str
    reasons: dict | None


@dataclass(frozen=True)
class LogCoverage:
    status: str
    watched_from: str | None
    last_read_at: str | None
    gaps: list[LogGap]
    failed_reads_in_window: int


@dataclass(frozen=True)
class RankedAccount:
    """One account row as read_ranked_stats returns it."""
    player_tag: str
    member_id: int | None
    player_name: str | None
    clan_tag: str | None
    status: str
    selection_rank: int | None
    ranking: dict
    ranking_observed_at: str | None
    attacks_used: int | None
    defenses: int | None
    attacks_available: int | None
    daily: list[DailyBattles]
    battle_records: int
    counts_complete: bool
    log_coverage: LogCoverage
    first_observed_at: str | None
    last_observed_at: str | None


def ranked_reads(context):
    return getattr(getattr(context, 'bot', None), 'ranked_reads', None)


def iso(value):
    return None if value is None else datetime.fromtimestamp(value / SECOND, timezone.utc).isoformat()


def micros(value):
    return round(value.timestamp() * SECOND)


def coverage_view(coverage):
    return {"status": coverage["status"], "watched_from": iso(coverage["watched_from"]),
            "last_read_at": iso(coverage["last_read_at"]),
            "gaps": [{**gap, "after": iso(gap["after"]), "before": iso(gap["before"])} for gap in coverage["gaps"]],
            "failed_reads_in_window": coverage["failed_reads_in_window"]}


def account_view(row):
    coverage = row["log_coverage"]
    result = {key: row[key] for key in (
        "player_tag", "member_id", "player_name", "clan_tag", "status", "selection_rank", "ranking",
        "attacks_used", "defenses", "daily", "battle_records", "counts_complete")}
    result.update(ranking_observed_at=iso(row["ranking_observed_at"]), attacks_available=None,
                  log_coverage=coverage_view(coverage),
                  # The span the log was read in, so a short span shows as a shorter window.
                  first_observed_at=iso(coverage["watched_from"]), last_observed_at=iso(coverage["last_read_at"]))
    if "battles" in row:
        result["battles"] = [{**battle, "battle_at": iso(battle["battle_at"])} for battle in row["battles"]]
    return result


def history_coverage(result, after, before):
    accounts = result.get("accounts", [])
    reasons = []
    if not accounts or not any(row.get("battle_records", 0) for row in accounts):
        reasons.append("empty_ranked_history")
    if any(row.get("counts_complete") is False for row in accounts):
        reasons.append("incomplete_ranked_counts")
    if any(row.get("log_coverage", {}).get("failed_reads_in_window") for row in accounts):
        reasons.append("failed_battle_log_reads")
    causes = {gap["cause"] for row in accounts for gap in row.get("log_coverage", {}).get("gaps", [])}
    reasons.extend("battle_log_" + cause for cause in sorted(causes))
    if result.get("coverage_complete") is False or result.get("complete") is False:
        reasons.append("incomplete_ranked_history")
    if result.get("unobserved_selections"):
        reasons.append("unobserved_selections")
    if result.get("next_offset") is not None:
        reasons.append("more_accounts_available")
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


async def read_window(context, selector, values, after, before, *, offset=0, limit=25, include_battles=False):
    """The record's ranked window for a checked selection; raises on an unreadable record."""
    reads = ranked_reads(context)
    dependencies = getattr(context, 'read_dependencies', None)
    if dependencies is not None:
        revision = (await asyncio.to_thread(source_revision, reads)
                    if getattr(context, 'read_cache', None) is not None else None)
        dependencies.observe(context, reads, revision)
    return await reads.window(selector, values, after=micros(after), before=micros(before),
                              offset=offset, limit=limit, include_battles=include_battles)


async def read_ranked_stats(context, arguments, *, maximum=25, include_battles=False, selection_maximum=200):
    await require_evidence_access(context)
    if ranked_reads(context) is None:
        return {"error": "Ranked observations are not available."}
    selectors = [key for key in ("member_ids", "account_tags", "clan_tags") if arguments.get(key)]
    if len(selectors) != 1:
        return invalid_arguments("Choose members, accounts or clans.", "member_ids", "account_tags", "clan_tags")
    selector = selectors[0]
    values = arguments[selector]
    if not isinstance(values, list) or not 1 <= len(values) <= selection_maximum:
        return invalid_arguments("Choose between one and two hundred members, accounts or clans.", selector)
    if selector == "member_ids":
        if any(type(value) is not int or value <= 0 for value in values):
            return invalid_arguments("Supply valid member IDs.", "member_ids")
    else:
        normalized = []
        for value in values:
            clan = CLANS.get(value.upper()) if isinstance(value, str) and selector == "clan_tags" else None
            tag = normalize_player_tag(clan.tag if clan else value)
            if tag is None:
                return invalid_arguments("Use valid Clash tags or family clan codes.", selector)
            normalized.append(tag)
        values = normalized
    try:
        after = search_date(arguments.get("after"))
        before = search_date(arguments.get("before")) or datetime.now(timezone.utc)
        if after is None or after >= before:
            raise ValueError("Invalid window")
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 25)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= maximum:
            raise ValueError("Invalid page")
    except (ValueError, TypeError):
        return invalid_arguments("Use ISO dates with the end after the start and a valid page.",
                                 "after", "before", "offset", "limit")
    try:
        stored = await read_window(context, selector, list(dict.fromkeys(values)), after, before,
                                   offset=offset, limit=limit, include_battles=include_battles)
    except FAILURES:
        return {"error": "Ranked observations could not be read."}
    await require_evidence_access(context)
    result = {"after": after.isoformat(), "before": before.isoformat(),
              "accounts": [account_view(row) for row in stored["accounts"]],
              "total_accounts": stored["total_accounts"], "offset": stored["offset"],
              "next_offset": stored["next_offset"], "unobserved_selections": stored["unobserved_selections"],
              "complete": True, "coverage_complete": stored["coverage_complete"],
              "source": "family_record", "bases": stored["bases"], "limitations": list(LIMITATIONS)}
    reasons = history_coverage(result, after, before)
    if reasons:
        result.update(complete=False, history_coverage={
            "status": "empty" if "empty_ranked_history" in reasons else "partial",
            "message": ("Stored ranked history is empty for this selection."
                        if "empty_ranked_history" in reasons else
                        "Stored ranked history is incomplete for the requested window."),
            "requested_after": after.isoformat(), "requested_before": before.isoformat(),
            "reasons": reasons,
        }, flags={"status": "partial", "truncated": False, "limits": reasons})
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
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 25},
            }, "required": ["after"], "additionalProperties": False,
             RULES: [{"needs": SELECTORS, "error": "Choose members, accounts or clans."},
                     {"only_one": SELECTORS, "error": "Choose members, accounts or clans."}]},
            usage_note="Choose one selector. Attacks count stored ranked/Legend battles, including losses; "
                       "log_coverage gaps show unread logs. Dates UTC.",
        ), read_ranked_stats,
        contract=CapabilityContract(entity_fields=(("member_ids", "discord_member_set"),
            ("account_tags", "clash_account_set"), ("clan_tags", "clan_set"))),
    ),)

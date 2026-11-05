"""Bounded membership observations from the family record's clan stays.

Stays are the record's roster observations, with the old clan health file's
hourly stays copied into it for the time before the record began. An arrival is
when an account was first seen in a clan, not when it joined.
"""

from datetime import datetime, timedelta, timezone

from elbow_helper.configuration.clans import CLANS
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool
from ..research.history import search_date
from .. import record_identity

STAY_LIMIT = 100_000
GAP_SECONDS = 48 * 3600
# A stay that starts this soon after the last hourly capture of the account's copied old stay continues from it.
TRANSFER_SECONDS = 2 * 3600
SECOND = 1_000_000


def iso(value):
    return None if value is None else datetime.fromtimestamp(value / SECOND, timezone.utc).isoformat()


def membership_events(accounts, after, clan_codes):
    """Arrivals in the window from each account's stays (oldest first) seen inside the history bounds."""
    events = []
    for account in accounts:
        previous = None
        for stay in account["stays"]:
            # A record stay ends at the roster that first missed the account; an hourly copied one at its last capture.
            continued = previous is not None and (
                (previous["to_at"] is not None and stay["from_at"] <= previous["to_at"])
                or (previous["basis"] == "observed_hourly"
                    and stay["from_at"] - previous["last_seen_at"] <= TRANSFER_SECONDS * SECOND))
            if continued and previous["clan_code"] == stay["clan_code"]:
                # The same stay, carried from the old file's copy into the record.
                previous = {**stay, "from_at": previous["from_at"], "first_seen_at": previous["first_seen_at"]}
                continue
            if stay["from_at"] >= after and (not clan_codes or stay["clan_code"] in clan_codes):
                kind = ("family_transfer" if continued else
                        "returning_observation" if previous is not None else "first_observed")
                events.append({
                    "player_tag": account["player_tag"], "player_name": stay.get("player_name"),
                    "clan_code": stay["clan_code"], "kind": kind, "observed_at": iso(stay["from_at"]),
                    "previous_observation_at": iso(previous["last_seen_at"]) if previous else None,
                    "previous_clan_code": previous["clan_code"] if continued else None,
                    "prior_membership_observed_at": iso(previous["first_seen_at"]) if previous else None,
                    "arrival_time_known": False, "first_ever_membership_known": False,
                    "sighting_gap": previous is None or stay["from_at"] - previous["last_seen_at"] > GAP_SECONDS * SECOND,
                })
            previous = stay
    return sorted(events, key=lambda row: (row["observed_at"], row["player_tag"]), reverse=True)


async def read_membership_history(context, arguments):
    await require_evidence_access(context)
    try:
        after = search_date(arguments.get("after"))
        before = search_date(arguments.get("before")) or datetime.now(timezone.utc)
        history_after = search_date(arguments.get("history_after")) or (
            max(after - timedelta(days=30), before - timedelta(days=90)) if after else None)
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 25)
        codes = arguments.get("clan_codes", [])
        if (after is None or history_after is None or not history_after <= after < before
                or before - history_after > timedelta(days=90)
                or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 25
                or not isinstance(codes, list) or len(codes) > 20 or any(code not in CLANS for code in codes)):
            raise ValueError("Invalid membership selection")
    except (ValueError, TypeError, AttributeError):
        return {"error": "Use ISO dates within ninety days, valid clan codes and a page of 1..25.",
                "cause": "invalid_arguments"}
    start, end = int(history_after.timestamp() * SECOND), int(before.timestamp() * SECOND)
    observed = await record_identity.record(context, "stay_reads", "movements", after=start, before=end,
                                            limit=STAY_LIMIT)
    if observed is None:
        return {"error": "Membership observations could not be read within their bounds.",
                "cause": "source_unavailable"}
    if observed["truncated"]:
        return {"error": "Membership observations exceed the row bound; narrow the window.",
                "cause": "source_unavailable"}
    for account in observed["rows"]:
        # Only what was seen inside the history bounds counts as an earlier observation.
        account["stays"] = [{**stay, "first_seen_at": max(stay["first_seen_at"], start)} for stay in account["stays"]]
    events = membership_events(observed["rows"], int(after.timestamp() * SECOND), codes)
    await require_evidence_access(context)
    selected = events[offset:offset + limit]
    next_offset = offset + len(selected) if offset + len(selected) < len(events) else None
    seen = [stay[key] for account in observed["rows"] for stay in account["stays"]
            for key in ("first_seen_at", "last_seen_at")]
    return {
        "source": "family_clan_stays", "events": selected,
        "matching_rows": len(events), "next_offset": next_offset, "truncated": False,
        "coverage": {
            "requested_after": after.isoformat(), "requested_before": before.isoformat(),
            "history_after": history_after.isoformat(), "before_exclusive": True,
            "observed_after": iso(min(seen)) if seen else None,
            "observed_before": iso(max(seen)) if seen else None,
            "stay_count": sum(len(account["stays"]) for account in observed["rows"]),
            "history_complete": False, "first_ever_membership_known": False,
        },
        "limitations": {"observations_only": True, "arrivals_between_observations": True,
                        "earlier_membership_outside_window_unknown": True,
                        "before_record_hourly_observations": True},
        "flags": {"status": "partial", "truncated": False, "limits": ["bounded_membership_observations"]},
    }


def membership_tools():
    return (RegisteredAgentTool(AgentToolDefinition(
        "read_membership_history", "Read bounded newcomer and returner observations from family clan stays.",
        {"type": "object", "properties": {
            "after": {"type": "string", "maxLength": 40},
            "before": {"type": "string", "maxLength": 40},
            "history_after": {"type": "string", "maxLength": 40},
            "clan_codes": {"type": "array", "maxItems": 20,
                           "items": {"type": "string", "enum": sorted(CLANS)}},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 25},
        }, "required": ["after"], "additionalProperties": False},
        usage_note="Max 90 days of family clan stays. First observed is not first-ever membership; returns require earlier observations inside bounds.",
    ), read_membership_history, contract=CapabilityContract()),)

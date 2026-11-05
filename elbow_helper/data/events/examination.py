"""Examination cases: one row per open case; the roster and panel live in workflow state."""

import json

from .records import now_us


WORKFLOW = "examination"
# Case fields that name a member other than the applicant.
MEMBER_FIELDS = ("availability_set_by", "route_change_requested_by", "responded_by")


def load_cases(connection):
    return {str(channel_id): json.loads(case_json) for channel_id, case_json in connection.execute(
        "SELECT ticket_channel_id,case_json FROM examination_cases ORDER BY ticket_channel_id")}


def save_case(connection, channel_id, case):
    """Write or, for None, remove one case inside the caller's transaction."""
    if type(channel_id) is not int or not 0 < channel_id < 2**63 or not connection.in_transaction:
        raise ValueError("Examination cases require a ticket channel and transaction")
    if case is None:
        connection.execute("DELETE FROM examination_cases WHERE ticket_channel_id=?", (channel_id,))
        return
    connection.execute(
        "INSERT INTO examination_cases(ticket_channel_id,case_json,updated_at) VALUES (?,?,?) "
        "ON CONFLICT(ticket_channel_id) DO UPDATE SET case_json=excluded.case_json,"
        "updated_at=excluded.updated_at",
        (channel_id, json.dumps(case, sort_keys=True, separators=(",", ":")), now_us()))


def scrub_member(cases, meta, discord_id):
    """Drop a member's own cases and examiner profile and clear their ID elsewhere.

    Returns the keys of the cases that changed."""
    changed = set()
    for key in list(cases):
        case = cases[key]
        if not isinstance(case, dict):
            continue
        if str(case.get("opener_id")) == str(discord_id):
            del cases[key]
            changed.add(key)
            continue
        before = json.dumps(case, sort_keys=True)
        for field in MEMBER_FIELDS:
            if str(case.get(field)) == str(discord_id):
                case[field] = None
        if isinstance(case.get("pinged_ids"), list):
            case["pinged_ids"] = [value for value in case["pinged_ids"] if str(value) != str(discord_id)]
        draft = case.get("availability_draft")
        if isinstance(draft, dict) and str(draft.get("user_id")) == str(discord_id):
            draft.pop("user_id")
        if json.dumps(case, sort_keys=True) != before:
            changed.add(key)
    for part in ("examiner_roster", "timezone_preferences"):
        if isinstance(meta.get(part), dict):
            meta[part].pop(str(discord_id), None)
    return changed

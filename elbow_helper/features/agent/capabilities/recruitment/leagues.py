"""Ranked leagues for a verified recruitment or join cohort, across all linked accounts."""

import re

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...access import require_evidence_access
from ...datasets.clash import read_clash, RANKED_LEAGUE_FIELDS
from ...research.history import search_date
from ...engine.capability_contract import CapabilityContract
from ...models import RegisteredAgentTool
from ...reports.tools import authorize_report
from ..account_links.reads import read_linked_accounts
from ..member_lifecycle.report import MemberLifecycleReport
from .report import RecruitmentTrialReport


def _league_key(value):
    return re.sub(r"[\s_-]+", "", re.sub(r"\bleague\b", "", value.casefold()))


async def read_cohort_leagues(context, arguments):
    await require_evidence_access(context)
    report_id = arguments["cohort_report_id"]
    report = context.state.reports.get(report_id)
    if (not isinstance(report, (RecruitmentTrialReport, MemberLifecycleReport))
            or report.guild_id != context.guild.id):
        return {"error": "That recruitment or join report is not available."}
    if not await authorize_report(context, report_id):
        return {"error": "That recruitment or join report is not accessible."}
    try:
        after, before = search_date(arguments.get("after")), search_date(arguments.get("before"))
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 200)
        if (after and before and after >= before or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 200):
            raise ValueError("Invalid cohort page")
    except (TypeError, ValueError, AttributeError):
        return {"error": "Use ISO dates and a page of 1 to 200 accounts.", "cause": "invalid_arguments"}
    trials = isinstance(report, RecruitmentTrialReport)
    rows = report.snapshot.trials if trials else report.snapshot.rows
    members = sorted({row.applicant_member_id if trials else row.member_id for row in rows
                      if (after is None or search_date(row.start_at if trials else row.joined_at) >= after)
                      and (before is None or search_date(row.start_at if trials else row.joined_at) < before)})
    accounts, unlinked = [], []
    if members:
        linked = await read_linked_accounts(context, {"member_ids": members}, maximum=10_000)
        if "error" in linked:
            return linked
        accounts = linked["accounts"]
        unlinked = linked["unlinked_member_ids"]
    # All accounts remain attached to their actual member. A directory's first
    # observation or a clan roster cannot introduce extra cohort candidates.
    accounts = sorted({(row["member_id"], row["player_tag"]): row for row in accounts}.values(),
                      key=lambda row: (row["member_id"], row["player_tag"]))
    selected = accounts[offset:offset + limit]
    live = await read_clash(context, {"kind": "player", "tags": list(dict.fromkeys(
        row["player_tag"] for row in selected)), "detail": "summary"}) if selected else {"items": []}
    if "error" in live:
        return live
    profiles = {item["tag"]: item for item in live["items"]}
    league = _league_key(arguments.get("league", ""))
    result = []
    for account in selected:
        item = profiles[account["player_tag"]]
        data = item.get("data", {})
        tier = data.get("leagueTier") or {}
        tier = tier if isinstance(tier, dict) else {}
        name = tier.get("name")
        result.append({**account, "status": item["status"], "ranked_league": name,
                       "leagueTier": {key: tier[key] for key in RANKED_LEAGUE_FIELDS if key in tier},
                       "legacy_league": (data.get("league") or {}).get("name"),
                       "trophies": data.get("trophies"), "best_trophies": data.get("bestTrophies"),
                       "matches": (_league_key(name).startswith(league) if isinstance(name, str) else None)})
    next_offset = offset + len(selected) if offset + len(selected) < len(accounts) else None
    unknown = [row["player_tag"] for row in result if row["matches"] is None]
    matched = [row for row in result if row["matches"] is True]
    next_page = ({**arguments, "offset": next_offset} if next_offset is not None else None)
    await require_evidence_access(context)
    return {"source": "cohort_ranked_leagues", "cohort_report_id": report_id,
            "cohort": "active_trials" if trials else "current_stay_joins",
            "after": arguments.get("after"), "before": arguments.get("before"),
            "metric": "current_ranked_league", "league": league,
            "member_ids": members, "member_count": len(members), "account_count": len(accounts),
            "accounts": result, "unlinked_member_ids": unlinked, "unknown_account_tags": unknown,
            "matching_members_in_page": len({row["member_id"] for row in matched}),
            "matching_accounts_in_page": len(matched), "offset": offset, "next_offset": next_offset,
            "returned_account_count": len(selected),
            "remaining_account_count": max(0, len(accounts) - offset - len(selected)),
            "next_page_arguments": next_page,
            "instruction": ("This is one account page, not a complete cohort count. "
                            "Continue read_cohort_leagues with next_page_arguments or narrow the date filters. "
                            "Deduplicate members across account pages."
                            if next_page is not None else
                            "Combine all requested account pages before counting members. "
                            "Unlinked members and unknown leagues remain unverified."),
            "complete": offset == 0 and next_offset is None and not unknown and not unlinked,
            "field_meanings": {"ranked_league": "leagueTier.name, never legacy league or best trophies",
                               "cohort": "trial start or current Discord stay join dates, never account first_seen",
                               "matching_members_in_page": "unique Discord members with a matching linked account"}}


def cohort_league_tools():
    return (RegisteredAgentTool(AgentToolDefinition(
        "read_cohort_leagues", "Read ranked leagues for every linked account in a retained trial or join cohort.",
        {"type": "object", "properties": {
            "cohort_report_id": {"type": "string", "minLength": 1, "maxLength": 32},
            "after": {"type": "string", "maxLength": 40}, "before": {"type": "string", "maxLength": 40},
            "league": {"type": "string", "minLength": 1, "maxLength": 80},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 200},
        }, "required": ["cohort_report_id"], "additionalProperties": False},
        usage_note="Use an active-trial or member-lifecycle report. Dates filter trial starts/Discord joins. Continue next_offset; missing leagueTier is unknown, never unranked.",
    ), read_cohort_leagues, contract=CapabilityContract(
        entity_fields=(("cohort_report_id", "saved_report"),), retained_fields=("cohort_report_id",),
        source_scope="retained_channel_evidence", required_access=frozenset({"lead"}),
    )),)

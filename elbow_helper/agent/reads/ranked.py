"""One checked ranked selection, with summary/detail views over RankedReads only."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3
from typing import Literal

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigrationError
from elbow_helper.agent.safety.access import require_evidence_access
from ..research.history import search_date
from elbow_helper.agent.engine.validation import invalid_arguments
from elbow_helper.agent.reads.cache import source_revision
from .ranked_measures import METRICS, MODES, account_measures, cohort_measure

SECOND = 1_000_000
FAILURES = (OSError, sqlite3.Error, TimeoutError, SQLiteMigrationError, ValueError)
MAX_ACCOUNTS = 10_000
SELECTOR_ERROR = 'Choose members, accounts or clans.'
LIMITATIONS = ["selection_rank_uses_last_stored_trophies_within_selected_accounts",
               "attacks_used_counts_stored_ranked_and_legend_attacks_including_losses",
               "log_coverage_gaps_can_hide_battles", "attack_quota_unavailable",
               "daily_counts_use_battle_timestamp_UTC",
               "owner_is_current_bot_link_and_clan_is_membership_at_battle_time"]


@dataclass(frozen=True)
class RankedRequest:
    selector: Literal['member_ids', 'account_tags', 'clan_tags']
    values: tuple
    after: datetime
    before: datetime
    detail: bool
    mode: Literal['ranked_and_legend', 'ranked', 'legend']
    direction: Literal['attacks', 'defenses'] | None
    metric: Literal['average_stars', 'triple_rate_percent', 'average_destruction_percent'] | None


def checked_request(arguments, selection_maximum):
    selectors = [key for key in ('member_ids', 'account_tags', 'clan_tags') if arguments.get(key)]
    if len(selectors) != 1:
        return invalid_arguments(SELECTOR_ERROR, 'member_ids', 'account_tags', 'clan_tags')
    selector = selectors[0]
    values = arguments[selector]
    if not isinstance(values, list) or not 1 <= len(values) <= selection_maximum:
        return invalid_arguments('Choose between one and two hundred members, accounts or clans.', selector)
    if selector == 'member_ids':
        if any(type(value) is not int or value <= 0 for value in values):
            return invalid_arguments('Supply valid member IDs.', 'member_ids')
    else:
        normalized = []
        for value in values:
            clan = CLANS.get(value.upper()) if isinstance(value, str) and selector == 'clan_tags' else None
            tag = normalize_player_tag(clan.tag if clan else value)
            if tag is None:
                return invalid_arguments('Use valid Clash tags or family clan codes.', selector)
            normalized.append(tag)
        values = normalized
    try:
        after = search_date(arguments.get('after'))
        before = search_date(arguments.get('before')) or datetime.now(timezone.utc)
        offset, limit = arguments.get('offset', 0), arguments.get('limit', 500)
        if (after is None or after >= before or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 500):
            raise ValueError('Invalid window')
    except (ValueError, TypeError, AttributeError):
        return invalid_arguments('Use ISO dates with the end after the start and a valid page.',
                                 'after', 'before', 'offset', 'limit')
    mode, direction, metric = arguments.get('mode', 'ranked_and_legend'), arguments.get('direction'), arguments.get('metric')
    if (not isinstance(mode, str) or mode not in MODES
            or direction not in (None, 'attacks', 'defenses') or metric not in (None, *METRICS)
            or 'include_battles' in arguments and type(arguments['include_battles']) is not bool
            or metric is not None and direction is None or arguments.get('detail', 'summary') not in ('summary', 'detail')):
        return invalid_arguments('Choose a supported battle measure.', 'mode', 'direction', 'metric', 'detail')
    return RankedRequest(selector, tuple(dict.fromkeys(values)), after, before,
                         arguments.get('detail') == 'detail' or arguments.get('include_battles', False),
                         mode, direction, metric)


async def read_ranked(context, arguments, *, selection_maximum=200, starts=None):
    """Return the complete selected facts; family_tool owns retained paging and compact delivery.

    starts supplies per-account hibernation windows internally, never a second fact reader.
    """
    await require_evidence_access(context)
    if ranked_reads(context) is None:
        return {'error': 'Ranked observations are not available.'}
    if arguments.get('cohort_ref'):
        if arguments.get('member_ids'):
            return invalid_arguments(SELECTOR_ERROR, 'member_ids', 'cohort_ref')
        report = context.state.reports.get(arguments['cohort_ref'])
        if report is not None and getattr(report, 'guild_id', None) != context.guild.id:
            return {'error': 'That cohort is no longer available here. Read it again.', 'cause': 'report_unavailable'}
        from .projections import cohort_selection
        selection = await cohort_selection(context, arguments['cohort_ref'], 'account_tags')
        if 'error' in selection:
            return selection
        arguments = {**arguments, **selection}
        selection_maximum = MAX_ACCOUNTS
    request = checked_request(arguments, selection_maximum)
    if isinstance(request, dict):
        return request
    roster_coverage = None
    if request.selector == 'clan_tags' and arguments.get('membership_basis', 'community') == 'community':
        codes = [next((code for code, clan in CLANS.items() if clan.tag == value), None) for value in request.values]
        if all(codes) and any(CLANS[code].cwl_only for code in codes):
            from elbow_helper.agent.reads.identity_reads import clan_rosters
            roster = await clan_rosters(context, {'clan_codes': codes, 'basis': 'community',
                **({'season': arguments['season']} if arguments.get('season') else {})})
            if 'error' in roster:
                return roster
            tags = sorted({row['player_tag'] for row in roster['rows'] if row.get('player_tag')})
            if not tags:
                return {'error': 'That clan account cohort is not available.', 'cause': 'source_unavailable'}
            roster_coverage = roster['clans']
            request = checked_request({**{key: value for key, value in arguments.items() if key != 'clan_tags'},
                                       'account_tags': tags}, MAX_ACCOUNTS)
            if isinstance(request, dict):
                return request
    try:
        stored = await read_window(context, request.selector, list(request.values), request.after, request.before,
                                   limit=MAX_ACCOUNTS, include_battles=True)
    except FAILURES:
        return {'error': 'Ranked observations could not be read.'}
    await require_evidence_access(context)
    if stored.get('next_offset') is not None:
        return {'error': 'Choose a smaller ranked account cohort; the complete selection exceeds capacity.',
                'cause': 'capacity'}
    rows = []
    for raw in stored['accounts']:
        account = account_view(raw)
        battles = account.pop('battles', [])
        after = (datetime.fromtimestamp(starts[account['player_tag']], timezone.utc)
                 if starts and account['player_tag'] in starts else request.after)
        selected_battles, measures = account_measures(battles, after, request.before, request.mode)
        known = account['attacks_used'] is not None
        row = {**account, 'observed_attacks': measures['attacks'] if known else None,
               'observed_defenses': measures['defenses'] if known else None,
               'ranked_league': account.get('ranking', {}).get('leagueTier'),
               'watch_status': account['log_coverage']['status'],
               'watched_from': account['log_coverage']['watched_from'], 'historical_counts_complete': False}
        row['attacks_used'] = measures['attacks']['battles'] if known else None
        row['defenses'] = measures['defenses']['battles'] if known else None
        row['battle_records'] = measures['attacks']['battles'] + measures['defenses']['battles']
        if starts:
            attacks = [battle['battle_at'] for battle in selected_battles if battle['payload']['attack']]
            activity_known = bool(attacks) or row['watch_status'] != 'never_read'
            row['activity_since'] = {
                'observed_ranked_attacks_since_start': len(attacks) if activity_known else None,
                'latest_observed_ranked_attack_at': max(attacks) if attacks else None,
                'ranked_log_gaps_since_start': [{**gap, 'after': max(gap['after'], after.isoformat())}
                    for gap in row['log_coverage']['gaps'] if gap['before'] > after.isoformat()] if activity_known else None}
        if request.detail:
            row['battles'] = selected_battles
        if request.mode != 'ranked_and_legend' or starts:
            dates = {}
            for battle in selected_battles:
                daily = dates.setdefault(battle['battle_at'][:10], {'attacks_used': 0, 'defenses': 0})
                daily['attacks_used' if battle['payload']['attack'] else 'defenses'] += 1
            row['daily'] = [{'date': date, **counts} for date, counts in sorted(dates.items())]
        rows.append(row)
    rows.sort(key=lambda row: (row.get('selection_rank') is None, row.get('selection_rank') or 0, row['player_tag']))
    result = {'after': request.after.isoformat(), 'before': request.before.isoformat(),
              'rows': rows, 'total_accounts': stored['total_accounts'],
              'unobserved_selections': stored['unobserved_selections'], 'complete': True,
              'coverage_complete': stored['coverage_complete'], 'source': 'family_record',
              'bases': stored['bases'], 'limitations': list(LIMITATIONS), 'membership_coverage': roster_coverage}
    reasons = history_coverage({**result, 'accounts': rows}, request.after, request.before)
    if reasons:
        result.update(complete=False, history_coverage={
            'status': 'empty' if 'empty_ranked_history' in reasons else 'partial',
            'message': ('Stored ranked history is empty for this selection.' if 'empty_ranked_history' in reasons
                        else 'Stored ranked history is incomplete for the requested window.'),
            'requested_after': request.after.isoformat(), 'requested_before': request.before.isoformat(), 'reasons': reasons})
    result.update(source_complete=result['complete'], collection_limits=result['limitations'])
    result['coverage_note'] = ('Stored samples only: battles the family record holds from battle-log reads, '
        'including those the old ranked collector kept. log_coverage gaps show where a log was not read; '
        'a gap or an unread log cannot prove zero activity. Owners are current bot links; a battle\'s clan is '
        'the recorded clan membership at that time, or the clan the old collector first saw it under.')
    resolved = {'account_tags': sorted(row['player_tag'] for row in rows), 'after': result['after'],
                'before': result['before'], 'mode': request.mode, 'direction': request.direction, 'metric': request.metric}
    result['resolved_selection'] = resolved
    if request.direction:
        result['aggregate'] = cohort_measure(rows, request.direction, request.mode, request.after, request.before)
    if request.metric:
        result['measurement'] = {**resolved, 'field': 'aggregate.' + request.metric,
                                 'places': 4 if request.metric == 'average_stars' else 2}
    return result


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



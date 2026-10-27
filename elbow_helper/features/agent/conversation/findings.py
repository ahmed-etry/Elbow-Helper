"""Compact, authorized follow-up findings backed by retained report snapshots."""

from datetime import datetime, timezone
import json

from ..read_cache import source_revision


LIVE_FIGURE_GRACE_SECONDS = 3 * 60
FINDINGS_INSTRUCTION = (
    'Use prior_findings and the earlier delivered answer to answer continuations without repeating research. '
    'The earlier answer is a finding, not new source evidence. Read retained reports only for rows or details '
    'needed by this request; read new sources only for missing facts. Preserve the earlier plan scope and period. '
    'For a file of the unchanged retained table, use its report_id with view=count and delivery=csv or xlsx; '
    'the file contains every row without resending the table for composition. '
    'For a requested fresh check, use fresh=true on a new family selection, or max_age_seconds=0 on read_clash; '
    'do not reuse an old report as a fresh observation. Treat superseded findings as historical only. '
    'Live values retain their observation times. For any reused live figure older than three minutes, '
    'say "as of <time>" using its actual observation time in readable UTC, attached to the affected claim. '
    'Do not add a generic freshness line or present an old current war as current.'
)


def family_revisions(context, family):
    observed = getattr(context, 'read_cache', None) is not None
    sources = {}
    if family in ('read_war_attendance', 'read_cwl_results', 'read_member_accounts',
                  'read_member_cohort', 'read_clan_rosters'):
        sources['cwl_queries'] = source_revision(getattr(context, 'cwl_queries', None)) if observed else None
    if family in ('read_ranked_activity', 'read_member_accounts', 'read_member_cohort'):
        sources['ranked_queries'] = source_revision(getattr(context, 'ranked_queries', None)) if observed else None
    return {name: version for name, version in sources.items()
            if getattr(context, name, None) is not None}


def report_is_current(context, report):
    data = json.loads(report.payload)
    summary = data['summary']
    versions = summary.get('source_versions')
    if versions is None:
        return False
    return all(version is not None and version == source_revision(getattr(context, name, None))
               for name, version in versions.items())


def observation_times(value):
    """Inspect timestamp fields only; source prose is never interpreted."""
    times = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ('observed_at', 'ranking_observed_at') and isinstance(item, str):
                try:
                    parsed = datetime.fromisoformat(item)
                    if parsed.utcoffset() is not None:
                        times.add(parsed.astimezone(timezone.utc).isoformat())
                except ValueError:
                    pass
            elif isinstance(item, (dict, list)):
                times.update(observation_times(item))
    elif isinstance(value, list):
        for item in value:
            times.update(observation_times(item))
    return sorted(times)


def retained_finding(context, encoded):
    """Replace repeated family records with handles, never an unauthorized table.

    Non-family legacy evidence keeps the existing exact model replay behavior.
    A missing or superseded snapshot carries no usable handle or old raw rows.
    """
    try:
        record = json.loads(encoded)
        result = json.loads(record['result'])
        if not isinstance(result, dict) or result.get('kind') != 'family' or result.get('error'):
            return None
        identifier = result.get('report_id')
        report = context.state.reports.get(identifier)
        summary = json.loads(report.payload)['summary'] if report is not None else {}
        available = (getattr(report, 'family', None) == result.get('family')
                     and summary.get('requester_id') == context.member.id
                     and result.get('family') != 'read_current_war'
                     and report_is_current(context, report))
        times = observation_times(json.loads(report.payload)) if available else observation_times(result)
        asked = context.source_message.created_at
        older = [instant for instant in times if
                 (asked - datetime.fromisoformat(instant)).total_seconds() > LIVE_FIGURE_GRACE_SECONDS]
        live = result.get('family') in ('read_member_accounts', 'read_member_cohort',
            'read_clan_rosters', 'read_ranked_activity', 'read_current_war', 'read_discord_access') or (
            result.get('family') == 'read_cwl_results'
            and result.get('selection', {}).get('metric') in ('league', 'signup_pool', 'assigned_rosters'))
        finding = {'family': result.get('family'), 'selection': result.get('selection', {}),
                   'observed_at': result.get('observed_at', record.get('observed_at')),
                   'observation_times': times,
                   'live_values': live, 'dated_live_figures_required': live and bool(older),
                   'status': 'retained_snapshot' if available else 'superseded_or_unavailable'}
        if available:
            finding.update(report_id=identifier, total_rows=report.manifest()['total_rows'],
                           read_arguments={'report_id': identifier},
                           source_versions=summary.get('source_versions', {}))
        record['result'] = json.dumps(finding, ensure_ascii=False, separators=(',', ':'))
        record.pop('model_result', None)
        record['prior_finding'] = True
        return json.dumps(record, ensure_ascii=False, separators=(',', ':'))
    except (AttributeError, KeyError, TypeError, ValueError):
        return None

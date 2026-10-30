"""Finish typed activity reads and expose authorized cohort snapshots to the writer."""
import json
from dataclasses import replace

from ..families.core import FamilyReport
from ..families.model_view import family_model_view, record_rows
from ..families.cwl_summary import CWL_ANALYSIS_FIELDS
from ..reports.tools import authorize_report
from ..reports.base import MAX_REPORTS, MAX_REPORT_PAYLOAD_BYTES
from ..plan.results import compact_result, model_result
from . import budgets as limits


def restorable_reference(result):
    """Selected facts are writer evidence; an index still needs source reads."""
    return (result.get('family') == 'read_community_reference'
            and result.get('mode') in ('sections', 'domain_facts', 'implementation_reference'))


def share_discussion_rows(evidence):
    """Share exact overlapping channel messages, retaining every read's scope.

    Only complete authorized model projections are combined. Reports, source
    text and paging handles remain unchanged. Conflicting message versions
    remain separate evidence, rather than silently choosing one version.
    """
    projected = dict(evidence)
    channels = {}
    for identity, result in evidence.items():
        if (not isinstance(result, dict) or result.get('error')
                or result.get('family') != 'read_conversation_evidence'
                or result.get('mode') != 'channel_window'
                or not result.get('channel_id')
                or result.get('complete_snapshot') is not True
                or 'source_reads' in result):
            continue
        rows = record_rows(result.get('rows'))
        if (not isinstance(rows, list) or not rows or len(rows) != result.get('total_rows')
                or any(not isinstance(row, dict) or type(row.get('message_id')) is not int
                       for row in rows)):
            continue
        channels.setdefault(result['channel_id'], []).append((identity, result, rows))
    for reads in channels.values():
        if len(reads) < 2:
            continue
        messages = {}
        overlap = conflict = False
        for _, _, rows in reads:
            for row in rows:
                identity = row['message_id']
                previous = messages.get(identity)
                if previous is not None:
                    overlap = True
                    if json.dumps(previous, sort_keys=True) != json.dumps(row, sort_keys=True):
                        conflict = True
                else:
                    messages[identity] = row
        if not overlap or conflict:
            continue
        identity, first, _ = reads[0]
        rows = sorted(messages.values(), key=lambda row: (row.get('timestamp') or '', row['message_id']))
        shared = {key: first[key] for key in ('family', 'mode', 'channel_id', 'channel') if key in first}
        shared.update(rows=rows, total_rows=len(rows), returned_rows=len(rows), count=len(rows),
            complete_snapshot=True,
            coverage={'per_read': {key: result.get('coverage') for key, result, _ in reads}},
            source_reads={key: {**{field: value for field, value in result.items() if field != 'rows'},
                'message_ids': [row['message_id'] for row in records]}
                for key, result, records in reads})
        projected[identity] = compact_result(shared)
        for key, _, _ in reads[1:]:
            projected.pop(key)
    return projected


def cohort_review_evidence(evidence, authorized):
    """Describe retained tables to research without resending their records."""
    projected = dict(evidence)
    for identity, result in evidence.items():
        if not isinstance(result, dict):
            continue
        checked = authorized.get(identity, {})
        if checked.get('cause') == 'report_unavailable':
            projected[identity] = checked
            continue
        table = result.get('family') in ('read_cwl_results', 'read_war_attendance',
            'read_ranked_activity', 'read_clan_rosters', 'read_member_accounts') or (
            result.get('family') == 'read_member_cohort'
            and result.get('basis') in ('clan_rosters', 'role_holders', 'joined_discord'))
        if not table or result.get('complete_snapshot') is not True or result.get('error'):
            continue
        rows = result.get('rows', [])
        columns = (rows.get('columns', []) if isinstance(rows, dict) else
                   list(rows[0]) if rows and isinstance(rows[0], dict) else [])
        projected[identity] = {key: value for key, value in result.items() if key != 'rows'}
        projected[identity]['retained_table'] = {
            'total_rows': result.get('total_rows'), 'columns': columns,
            'complete_snapshot': True}
    return projected


async def complete_activity_reads(flow):
    """Resume checked channel cursors within the ordinary tool/time/evidence budgets."""
    for identity, result in list(flow.completed_results.items()):
        if (result.get('family') != 'read_member_cohort'
                or not result.get('activity_coverage')):
            continue
        while not result['activity_coverage']['chat_coverage']['source_reads_complete']:
            if flow.runner.tool_calls >= limits.MAX_TOOL_CALLS:
                break
            report = flow.context.state.reports.get(result.get('report_id'))
            if not isinstance(report, FamilyReport):
                break
            retained = flow.context.state.reports
            # A continuation adds a complete immutable snapshot. Preserve
            # the other selected evidence rather than evicting scores or wars
            # while trying to fill an explicitly unknown chat window.
            if (len(retained) >= MAX_REPORTS or sum(r.retained_bytes for r in retained.values())
                    + report.retained_bytes > MAX_REPORT_PAYLOAD_BYTES):
                break
            previous = json.loads(report.payload)['summary']['activity_coverage']['chat_coverage']
            step_id = 'activity_resume_' + str(flow.runner.tool_calls)
            step = {'id': step_id, 'capability': 'read_member_cohort', 'arguments': {
                'basis': result['basis'], 'activity_ref': report.report_id, 'view': 'count'},
                'depends_on': []}
            resumed = await flow.runner.run({'goal': 'Complete the selected activity snapshot',
                'effort': 'low', 'output': 'text', 'steps': [step]})
            candidate = resumed.get(step_id, {})
            if candidate.get('error') or not candidate.get('activity_coverage'):
                break
            result = candidate
            flow.completed_results[identity] = result
            flow.result_views[identity] = flow.runner.model_results_for(resumed)[step_id]
            flow.steps_by_id[step_id] = step
            latest = flow.context.state.reports.get(result.get('report_id'))
            if (not isinstance(latest, FamilyReport) or previous ==
                    json.loads(latest.payload)['summary']['activity_coverage']['chat_coverage']):
                break


async def cohort_writer_evidence(context, evidence, *, snapshots=None):
    """Restore complete authorized cohorts and their dependent measure tables."""
    projected = dict(evidence)
    populations = {}
    roster_signatures = {}
    for identity, result in evidence.items():
        if not isinstance(result, dict):
            continue
        cohort = (result.get('family') == 'read_member_cohort'
                  and result.get('basis') in ('clan_rosters', 'role_holders', 'joined_discord'))
        measures = result.get('family') in ('read_cwl_results', 'read_war_attendance', 'read_ranked_activity')
        rosters = result.get('family') == 'read_clan_rosters'
        discussion = result.get('family') == 'read_conversation_evidence'
        accounts = result.get('family') == 'read_member_accounts'
        if not (cohort or measures or accounts or rosters or discussion or restorable_reference(result)):
            continue
        identifier = result.get('report_id')
        report = context.state.reports.get(identifier)
        authorization = context
        if report is None and snapshots and identifier in snapshots:
            report, sources, requirements = snapshots[identifier]
            authorization = replace(context, state=replace(context.state,
                reports={**context.state.reports, identifier: report},
                report_sources={identifier: sources},
                report_access_requirements={identifier: requirements}))
        if (not isinstance(report, FamilyReport) or report.guild_id != context.guild.id
                or report.family != result.get('family')
                or json.loads(report.payload)['summary'].get('requester_id') != context.member.id
                or not await authorize_report(authorization, report.report_id)):
            projected[identity] = {'error': 'That cohort is no longer available here.',
                                   'cause': 'report_unavailable'}
            continue
        count = report.manifest()['total_rows']
        if (result.get('evidence_projection') == 'complete_requested_file'
                and result.get('artifact_status') == 'complete' and not result.get('rows')):
            # The reader has prepared the whole requested file. Its manifest
            # and earlier findings suffice for an export-only continuation.
            continue
        page = report.page(limit=max(1, count))
        if rosters:
            signature = json.dumps({'rows': page['rows'], 'clans': page.get('clans')},
                                   sort_keys=True, ensure_ascii=False)
            previous = roster_signatures.get(signature)
            if previous is not None:
                projected.pop(previous, None)
            roster_signatures[signature] = identity
        if cohort and result.get('basis') == 'clan_rosters':
            tags = frozenset(account['player_tag'] for row in page['rows']
                for account in row.get('accounts', ()) if account.get('player_tag'))
            if tags:
                populations[identity] = (tags, bool(page.get('activity_coverage')))
        # Export outcomes are attached after report retention. Restoring the
        # account table must preserve successful files and failed exports alike.
        page.update({key: result[key] for key in ('artifact', 'artifact_status', 'artifact_note')
                     if key in result})
        view = family_model_view(model_result(page))
        if report.family == 'read_cwl_results':
            view.update({field: page[field] for field in CWL_ANALYSIS_FIELDS if field in page})
        if discussion and isinstance(view.get('rows'), list):
            # Transport pages prioritize recent replies; the restored writer
            # snapshot reads the whole exchange in chronological order.
            view['rows'] = sorted(view['rows'], key=lambda row: row.get('message_id', 0))
        projected[identity] = compact_result(view)
    enriched = {tags for tags, activity in populations.values() if activity}
    for identity, (tags, activity) in populations.items():
        if not activity and tags in enriched:
            # The exact same current accounts now have owner and activity
            # context. Do not resend the larger, older role-only snapshot.
            del projected[identity]
    return share_discussion_rows(projected)

"""Finish typed activity reads and expose authorized cohort snapshots to the writer."""
import json
from dataclasses import replace

from elbow_helper.agent.reads.snapshots import FamilyReport, WHOLE_FAMILIES
from elbow_helper.agent.reads.evidence import family_model_view, record_rows
from elbow_helper.features.cwl.results_summary import CWL_ANALYSIS_FIELDS
from ..reports.tools import authorize_report
from ..plan.results import compact_result, model_result


def delivered_whole(result, use):
    """Whether this read's saved report is delivered whole for `use` (restore, uncapped or finishes):
    the lookup's own declaration."""
    declared = WHOLE_FAMILIES.get(result.get('family'))
    rule = getattr(declared, use, False)
    return rule(result) if callable(rule) else bool(rule)


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
        table = result.get('family') in ('read_cwl_results', 'read_war_participation',
            'read_ranked_activity', 'read_clan_rosters', 'read_member_accounts') or (
            result.get('family') == 'read_member_cohort'
            and result.get('basis') in ('clan_rosters', 'role_holders', 'joined_discord', 'selected'))
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


def exported_whole(result):
    """The step asked for the whole file, which holds every row; its manifest is the evidence."""
    return (result.get('evidence_projection') == 'complete_requested_file'
            and result.get('artifact_status') == 'complete' and not result.get('rows'))


def requested_spans(context, evidence, snapshots):
    """The rows each restored step asked for. Steps that page through one report share one copy:
    overlapping or adjacent pages go to the first of those steps, the others map to None.
    A continuation page is restored with its report even though its view repeats no summary."""
    restored = {result.get('report_id') for result in evidence.values()
                if isinstance(result, dict) and result.get('report_id') and delivered_whole(result, 'restore')}
    pages = {}
    for identity, result in evidence.items():
        if (not isinstance(result, dict) or result.get('report_id') not in restored
                or exported_whole(result)):
            continue
        identifier = result.get('report_id')
        report = context.state.reports.get(identifier) or (snapshots or {}).get(identifier, (None,))[0]
        rows = report.requested_rows(result) if isinstance(report, FamilyReport) else None
        if rows is not None:
            pages.setdefault(identifier, []).append((identity, *rows))
    spans = {}
    for steps in pages.values():
        order = {identity: index for index, (identity, _, _) in enumerate(steps)}
        merged = []
        for identity, start, end in sorted(steps, key=lambda step: step[1:]):
            if merged and start <= merged[-1][2]:
                merged[-1][0].append(identity)
                merged[-1][2] = max(merged[-1][2], end)
            else:
                merged.append([[identity], start, end])
        for identities, start, end in merged:
            first = min(identities, key=order.get)
            spans.update({identity: (start, end) if identity == first else None for identity in identities})
    return spans


async def cohort_writer_evidence(context, evidence, *, snapshots=None):
    """Restore what each authorized step asked for, whole beyond the page shown between steps."""
    projected = dict(evidence)
    populations = {}
    roster_signatures = {}
    spans = requested_spans(context, evidence, snapshots)
    for identity, result in evidence.items():
        if not isinstance(result, dict):
            continue
        cohort = result.get('family') == 'read_member_cohort'
        rosters = result.get('family') == 'read_clan_rosters'
        discussion = result.get('family') == 'read_conversation_evidence'
        if not delivered_whole(result, 'restore') and identity not in spans:
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
        if exported_whole(result):
            # The reader has prepared the whole requested file. Its manifest
            # and earlier findings suffice for an export-only continuation.
            continue
        if identity in spans and spans[identity] is None:
            # An earlier step's restore holds these rows of the same report.
            projected.pop(identity, None)
            continue
        # The restored page states what it holds, as the step's own page did.
        if identity in spans:
            start, end = spans[identity]
            page = report.page(start, max(1, end - start))
            if page['next_offset'] is not None:
                page['limit'] = end - start
        else:
            page = {**report.page(view=result.get('view', 'rows')), 'view': result.get('view', 'rows')}
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
        view = family_model_view(model_result(page), shared_rows=False)
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

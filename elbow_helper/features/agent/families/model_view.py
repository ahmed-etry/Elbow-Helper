"""Compact typed family evidence before generic result size limits remove rows."""

from elbow_helper.configuration.roles import AGENT_TESTER_ROLE_ID
from ..research.history import readable_history_row
from .cwl_summary import CWL_ANALYSIS_FIELDS


def pick(value, fields):
    return {key: value[key] for key in fields if key in value}


def record_rows(value):
    if isinstance(value, dict) and set(value) == {'columns', 'rows'}:
        return [dict(zip(value['columns'], row)) for row in value['rows']]
    return value


def account_view(row, *, nested=False):
    result = pick(row, ('player_tag', 'player_name', 'townHallLevel', 'clan', 'clan_code', 'donations', 'war_stars', 'ranked_league', 'role',
                       'profile_status', 'war_activity', 'account_purpose')) if nested else dict(row)
    if isinstance(result.get('roles'), list):
        result['roles'] = [role for role in result['roles'] if role.get('role_id') != AGENT_TESTER_ROLE_ID]
    for field in ('ranked_league', 'legacy_league'):
        if isinstance(result.get(field), dict):
            result[field] = pick(result[field], ('id', 'name'))
    if isinstance(result.get('clan'), dict):
        result['clan'] = pick(result['clan'], ('tag', 'name'))
    return result


def roster_role_view(row):
    return pick(row, ('member_id', 'member_name', 'player_tag', 'player_name',
                      'membership_status', 'home_role_checks', 'home_clans', 'home_clan_status', 'profile_status'))


def ranked_view(row):
    # A fixed schema lets the transport encode large selections once as a
    # column table, including accounts whose names or ranking are unknown.
    fields = ('player_tag', 'member_id', 'player_name', 'status', 'selection_rank',
        'attacks_used', 'attacks_available', 'counts_complete', 'ranked_league', 'live_status',
        'duplicate_copies_removed', 'conflicting_battles', 'unreadable_ranked_rows',
        'historical_counts_complete')
    result = {field: row.get(field) for field in fields}
    for field in ('observed_attacks', 'observed_defenses'):
        metric = row.get(field)
        if isinstance(metric, dict):
            result[field] = pick(metric, ('selection', 'battles', 'stars_total', 'triples',
                'missing_stars', 'missing_destruction', 'average_stars',
                'average_destruction_percent', 'triple_rate_percent'))
            result[field]['observed_range'] = metric.get('coverage', {}).get('available_range')
        else:
            result[field] = metric
    return result


def participation_view(clans, rows):
    """Page identity lists with their measure rows; counts retain their scope."""
    tags = {tag for row in rows for tag in (
        ([row['player_tag']] if row.get('player_tag') else [])
        + row.get('account_tags', []))}
    members = {row['member_id'] for row in rows
               if row.get('unit') == 'member' and row.get('member_id')}
    result = []
    for clan in clans:
        item = dict(clan)
        for status in ('warring', 'joined_without_attacking', 'unknown'):
            if status not in clan:
                continue
            identities = []
            for identity in record_rows(clan[status].get('identities', [])):
                if identity.get('member_id'):
                    selected = [tag for tag in identity.get('account_tags', []) if tag in tags]
                    if selected or identity['member_id'] in members:
                        identities.append({'member_id': identity['member_id'],
                            'account_tags': selected})
                elif identity.get('player_tag') in tags:
                    identities.append({'player_tag': identity['player_tag']})
            item[status] = {**clan[status], 'identities': identities}
        result.append(item)
    return result


def family_model_view(payload):
    """Keep every selected identity and computed metric; full detail stays retained."""
    data = dict(payload)
    family = data.get('family')
    rows = data.get('rows')
    if not isinstance(rows, list):
        return data
    if family == 'read_conversation_evidence' and data.get('mode') == 'channel_window':
        data.setdefault('message_content_complete', all(row.get('content_complete') for row in rows))
        data.setdefault('attachment_contents_read', False)
        data['rows'] = [readable_history_row(row) for row in rows]
    if isinstance(data.get('unknown_accounts'), list):
        projection = roster_role_view if 'home_role_comparisons' in data else account_view
        selected = {row.get('player_tag') for row in rows}
        data['unknown_accounts'] = [projection(row) for row in data['unknown_accounts']
                                   if row.get('player_tag') in selected]
    if isinstance(data.get('clan_facts'), list):
        data['clan_facts'] = [pick(row, ('clan_code', 'name', 'tag', 'operational_cwl_only',
            'configured_cwl_only', 'membership_basis', 'configured_membership_basis',
            'operational_policy_status', 'unknown_policy')) for row in data['clan_facts']]
    if isinstance(data.get('performance_coverage'), dict):
        coverage = data['performance_coverage']
        data['performance_coverage'] = pick(coverage, ('coverage', 'resolved_selection', 'error', 'cause'))
        if 'war_ids' in coverage:
            data['performance_coverage']['wars_checked'] = len(coverage['war_ids'])
    if isinstance(data.get('activity_coverage'), dict):
        data.pop('provenance', None)
        coverage = data['activity_coverage']
        data['activity_coverage'] = {**coverage, 'chat_coverage': {
            key: value for key, value in coverage.get('chat_coverage', {}).items()
            if key not in ('channel_ids', 'channel_cursors', 'unavailable_channel_ids')}}
    if family == 'read_clan_rosters':
        for clan in data.get('clans', []):
            if isinstance(clan.get('profile'), dict):
                clan['profile'] = pick(clan['profile'], ('clan_code', 'purpose', 'branch',
                    'is_utility', 'configured_cwl_only', 'home_placement_eligible',
                    'home_placement_eligibility_basis', 'operational_status', 'operational_cwl_only',
                    'membership_basis', 'has_placeholders', 'town_hall_min', 'town_hall_max'))
    if family == 'read_ranked_activity':
        data['rows'] = [ranked_view(row) for row in rows]
        data['detail_projection'] = 'All selected account metrics; battle payloads and daily poll detail remain in the retained report.'
        data['ranked_modes'] = ['RANKED', 'LEGEND']
    elif family in ('read_member_accounts', 'read_member_cohort', 'read_recruitment_pipeline'):
        projected = []
        for row in rows:
            item = account_view(row)
            if family == 'read_member_accounts' and not data.get('selection', {}).get('include_roles'):
                # Role changes use their own current-role preview. Repeating
                # every cosmetic role on every alt obscures account evidence.
                item.pop('roles', None)
            if 'accounts' in item:
                item['member_name'] = item.get('member_name') or next((account.get('member_name')
                    for account in item['accounts'] if account.get('member_name')), None)
                item['accounts'] = [account_view(account, nested=True)
                                    for account in item['accounts']]
                if family == 'read_member_cohort' and data.get('basis') == 'clan_rosters':
                    item['accounts'] = [pick(account, ('clan_code', 'player_tag', 'player_name', 'donations',
                        'townHallLevel', 'role', 'account_purpose', 'war_activity'))
                                        for account in item['accounts']]
            if family == 'read_member_cohort' and data.get('basis') == 'role_holders':
                selected_roles = set(data.get('selected_role_ids', ()))
                item['roles'] = [role for role in item.get('roles', []) if role.get('role_id') in selected_roles]
            if family == 'read_member_cohort' and data.get('basis') in ('accepted_recruitment', 'clan_rosters'):
                item.pop('roles', None)
                if data.get('basis') == 'clan_rosters':
                    item.pop('joined_at', None)
                    item.pop('nickname', None)
            if family == 'read_recruitment_pipeline':
                for field in ('start_ts', 'expected_end_ts', 'remaining_seconds', 'overdue_seconds'):
                    item.pop(field, None)
                if isinstance(item.get('war_attendance'), dict):
                    item['war_attendance'] = pick(item['war_attendance'], ('attacks_used', 'attacks_expected',
                        'missed', 'unconfirmed_opportunities', 'totals_complete'))
                if isinstance(item.get('attack_performance'), dict):
                    item['attack_performance'] = {kind: {
                        key: pick(value, ('numerator', 'denominator')) if isinstance(value, dict) else value
                        for key, value in metrics.items() if key != 'accounts'}
                        for kind, metrics in item['attack_performance'].items()}
            projected.append(item)
            if family == 'read_member_cohort' and data.get('activity_coverage'):
                projected[-1] = pick(item, ('member_id', 'member_name', 'membership_status',
                    'home_clans', 'home_clan_status', 'home_clan_basis', 'leadership', 'accounts', 'account_tags', 'account_names', 'clan_codes',
                    'donations', 'war_status', 'wars_observed', 'attacks_used',
                    'attacks_expected', 'missed', 'attack_completion_percent',
                    'war_totals_complete', 'chat_status', 'latest_chat_at'))
        data['rows'] = projected
        if family == 'read_member_accounts' and 'home_role_comparisons' in data:
            # These comparisons are a projection of the same account rows,
            # not a second indivisible copy of the complete roster header.
            data['home_role_comparisons'] = [roster_role_view(row) for row in projected]
    elif family == 'read_hibernation':
        data['rows'] = [pick(row, ('member_id', 'member_name', 'joined_at', 'started_at',
            'started_ts', 'start_time_status', 'accounts', 'activity_coverage',
            'war_activity_coverage', 'ownership_basis')) for row in rows]
    if family == 'read_war_attendance' or (
            family == 'read_cwl_results' and isinstance(data.get('clan_participation'), list)):
        # Alias reconciliation detail is retained; model receives the checked
        # coverage and account metrics instead of repeated source identities.
        data['wars_checked'] = len(data.get('war_ids', []))
        data.pop('war_aliases', None)
        # Names and account details already live on the paged measure rows.
        # Repeating every selected identity in each page makes the header
        # indivisible. Project it to this page; full restoration projects all
        # rows and therefore restores every membership without another read.
        data['clan_participation'] = participation_view(data.get('clan_participation', []), rows)
        data['participation_scope'] = {
            'counts': 'full selected report', 'identities': 'accounts on this page'}
        data['rows'] = [dict(row, wars_observed=len(row['war_ids']))
                        if isinstance(row.get('war_ids'), list) else dict(row) for row in rows]
        for row in data['rows']:
            row.pop('war_ids', None)
    if family == 'read_cwl_results':
        # Retain computed full-selection analysis for final composition without
        # growing every transport page's fixed header with a second full table.
        for field in CWL_ANALYSIS_FIELDS:
            data.pop(field, None)
        # Scores and settings describe the requested account table. Per-attack
        # examples are retained detail, and otherwise crowd out every table row.
        data.pop('attack_sample', None)
        if isinstance(data.get('clan_results'), list):
            data['clan_results'] = [{key: value for key, value in summary.items()
                                    if key != 'attack_sample'} for summary in data['clan_results']]
            for summary in data['clan_results']:
                if isinstance(summary.get('top_rows'), list):
                    # These are per-clan navigation summaries. Full score
                    # components remain on the paged account rows and report.
                    summary['top_rows'] = [pick(row, ('player_tag', 'player_name', 'rank',
                        'ass_score', 'score', 'attacks', 'attacks_expected', 'stars'))
                        for row in summary['top_rows']]
    return data

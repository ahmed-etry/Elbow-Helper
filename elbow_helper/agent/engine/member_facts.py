"""Project trusted reference schemas, never classify or rewrite source prose."""

from copy import deepcopy

from ..access import ACCESS_ROLE_SETS, READ_ACCESS_ROLE_SETS
from elbow_helper.configuration.roles import CO_APPLICANT_ROLE_ID, ELDER_ROLE_ID, MEMBER_ROLE_ID


READ_TOPICS = {
    'read_war_attendance': 'War participation and missed attacks',
    'read_cwl_results': 'CWL results, leagues and ASS scores',
    'read_current_war': 'Current war lineups and attacks',
    'read_member_accounts': 'Members, linked accounts and home clans',
    'read_clan_rosters': 'Clan rosters and capacity',
    'read_member_cohort': 'Role holders and member activity',
    'read_recruitment_pipeline': 'Applications and trials',
    'read_hibernation': 'Hibernating members',
    'read_promotion_cases': 'Examinations and promotions',
    'read_community_reference': 'Community rules and procedures',
    'read_conversation_evidence': 'Conversation history and shared attachments',
    'read_ranked_activity': 'Ranked attacks and results',
    'read_discord_access': 'Role permissions and channel access',
}


def reader_roles(guild, requirements):
    """Each requirement is an alternative set of actual current role names."""
    return [sorted({role.name for role in getattr(guild, 'roles', ())
                    if role.id in READ_ACCESS_ROLE_SETS.get(level, ()) and getattr(role, 'name', None)})
            for level in sorted(requirements)]


def read_topics(overview):
    return {'topics': [READ_TOPICS[row['capability']] for row in overview.get('reads', ())
                       if row.get('capability') in READ_TOPICS]}


def _rows(payload):
    from ..families.model_view import record_rows
    return record_rows(payload.get('rows', []))


def role_facts(payload, guild):
    """Resolve aliases emitted by the trusted role-semantics producer only."""
    aliases = {'Core': ACCESS_ROLE_SETS['core'], 'Lead': ACCESS_ROLE_SETS['lead'],
               'Lead Plus': ACCESS_ROLE_SETS['lead_plus'], 'Elder': {ELDER_ROLE_ID},
               'Member': {MEMBER_ROLE_ID}, 'Co Applicant only': {CO_APPLICANT_ROLE_ID}}

    def names(alias):
        if alias not in aliases:
            return alias
        return sorted({role.name for role in getattr(guild, 'roles', ())
                       if role.id in aliases[alias] and getattr(role, 'name', None)})

    return {'rows': [{'subject': names(row['subject']),
        'holds': [names(value) for value in row.get('holds', ())],
        **({'alternatives': [names(value) for value in row['lead_plus_alternatives']]}
           if 'lead_plus_alternatives' in row else {})} for row in _rows(payload)]}


_REFERENCE_METADATA = frozenset({
    'sources', 'source', 'file_name', 'title', 'section_id', 'visibility', 'part', 'parts',
    'authority', 'approved_revision', 'permission_authority', 'coverage', 'mode',
    'reference_kind', 'semantic_facts_grant_permissions', 'unknown_policy',
})


def _reference(value):
    if isinstance(value, list):
        return [_reference(item) for item in value]
    if isinstance(value, dict):
        return {key: _reference(item) for key, item in value.items()
                if key not in _REFERENCE_METADATA or key == 'source' and isinstance(item, str)
                and item.startswith(('https://', 'http://'))}
    return deepcopy(value)


def workflow_facts(payload, guild):
    """Select member facts rather than handing the writer engineering commentary."""
    rows = []
    for row in _rows(payload):
        kind = row.get('workflow')
        if kind == 'account_linking':
            rows.append({'account_linking': {key: deepcopy(value) for key, value in row.items()
                if key not in ('sources', 'workflow')}})
        elif kind == 'ass_scoring':
            rows.append({key: deepcopy(value) for key, value in row.items()
                         if key not in ('sources', 'workflow')})
        elif kind == 'ticket_opening':
            support = row.get('support', {})
            rows.append({'support': {key: deepcopy(support[key]) for key in (
                'command', 'options', 'automatic_welcome', 'separate_welcome_command_required')
                if key in support}, 'recruitment': deepcopy(row.get('recruitment', {}))})
            rows[-1]['support']['operator_roles'] = reader_roles(guild, {'lead'})
        elif kind == 'roster_signup_management':
            removal = row.get('removal', {})
            controls = {key: deepcopy(removal[key]) for key in (
                'entry_point', 'control', 'selection', 'confirm_control') if key in removal}
            controls['operator_roles'] = reader_roles(guild, {'lead_plus'})
            controls['requester_allowed'] = removal.get('permission', {}).get('requester_allowed')
            rows.append({'roster_signup_management': {
                'scope': row.get('scope'), 'removal': controls,
                'self_service': deepcopy(row.get('self_service', {}))}})
        elif kind == 'recruit_placement':
            rows.append({key: deepcopy(row[key]) for key in (
                'new_applicant_route', 'acceptance_command', 'acceptance_options',
                'default_trial_days', 'confirmation_required',
                'clan_promotion_ticket_required_for_new_applicant', 'existing_member_promotion')
                if key in row})
            rows[-1]['clan_option_grants_approval'] = False
            rows[-1]['command_clan_options'] = deepcopy(row.get('supported_clan_codes', []))
        elif kind == 'discord_transfer_requests':
            rows.append({'discord_transfer_requests': {key: deepcopy(value) for key, value in row.items()
                if key not in ('sources', 'workflow')}})
        elif kind == 'recruitment_history':
            rows.append({'recruitment_history': {key: deepcopy(value) for key, value in row.items()
                if key not in ('sources', 'workflow')}})
        elif kind == 'roster_administration':
            rows.append({key: deepcopy(value) for key, value in row.items()
                         if key not in ('sources', 'workflow', 'operator_role_ids')})
            rows[-1]['operator_roles'] = [role.name for role in getattr(guild, 'roles', ())
                                         if role.id in row.get('operator_role_ids', ())]
        elif isinstance(row.get('schedule'), dict) and isinstance(row.get('effects'), list):
            rows.append({'task': kind.replace('_', ' '), **{
                key: deepcopy(value) for key, value in row.items() if key not in ('sources', 'workflow')}})
    return {'rows': rows}


def reference_facts(payload, guild):
    payload = {**payload, 'rows': _rows(payload)}
    kind = payload.get('selection', {}).get('reference_kind', payload.get('reference_kind'))
    if kind == 'bot_workflows' or payload.get('mode') == 'implementation_reference':
        return workflow_facts(payload, guild)
    facts = _reference(payload)
    if kind == 'clans':
        for row in facts.get('rows', ()):
            for field in ('configured_cwl_only', 'configured_membership_basis', 'operational_policy_status'):
                row.pop(field, None)
    if isinstance(payload.get('implementation_reference'), dict):
        facts['procedures'] = workflow_facts(payload['implementation_reference'], guild)
        facts.pop('implementation_reference', None)
    return role_facts(payload, guild) if kind == 'roles' else facts

"""Public owner context for account-level observations and recommendations."""

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.roles import (
    CORE, LEAD, CO_APPLICANT_ROLE_ID, SLEEPING_CO_ROLE_ID, SLEEPING_CROSS_ROLE_ID,
    MEMBERS, APPLICANT_ROLE_ID, ELDER_ROLE_ID, HIBERNATING_ROLE_ID,
    RECRUITERS, CWL_HELPERS, PLANNING_HELPERS, EXAMINERS,
)
from ..clan_roles import ROLE_PURPOSES, clan_role_purposes


PROFILE_ROLE_GROUPS = {
    'clan': {getattr(clan, field) for clan in CLANS.values() for field in ROLE_PURPOSES
             if getattr(clan, field) is not None},
    'leadership': CORE | LEAD | {CO_APPLICANT_ROLE_ID, SLEEPING_CO_ROLE_ID, SLEEPING_CROSS_ROLE_ID},
    'family': MEMBERS | {APPLICANT_ROLE_ID, ELDER_ROLE_ID, HIBERNATING_ROLE_ID},
    'recruitment': RECRUITERS,
    'war_and_coaching': CWL_HELPERS | set(PLANNING_HELPERS) | EXAMINERS,
}
PROFILE_ROLE_IDS = set().union(*PROFILE_ROLE_GROUPS.values())


def configured_member_roles(member, *, requested_role_ids=()):
    """Configured roles plus roles explicitly selected for a holder read."""
    allowed = PROFILE_ROLE_IDS | set(requested_role_ids)
    return [{'role_id': role.id, 'name': role.name,
             'clan_purposes': clan_role_purposes(role.id)}
            for role in getattr(member, 'roles', ()) if role.id in allowed]


def owner_context(member, identifier):
    roles = getattr(member, 'roles', ())
    held = {role.id for role in roles}
    active = CORE | LEAD | {clan.leadership_role_id for clan in CLANS.values()
                                 if clan.leadership_role_id is not None}
    sleeping = {SLEEPING_CO_ROLE_ID, SLEEPING_CROSS_ROLE_ID}
    applicants = {CO_APPLICANT_ROLE_ID}
    return {
        'member_id': identifier,
        'member_name': member.display_name if member else None,
        'leadership': {
            'status': ('unknown' if member is None else 'active' if held & active
                       else 'sleeping' if held & sleeping
                       else 'applicant' if held & applicants else 'none'),
            'roles': [{'role_id': role.id, 'name': getattr(role, 'name', None)}
                      for role in roles if role.id in active | sleeping | applicants],
        },
    }


def account_owner_profiles(rows):
    """Move repeated Discord leadership out of account-level observations."""
    members = {}
    for row in rows:
        leadership = row.pop('leadership', None)
        identifier = row.get('member_id')
        if identifier is None:
            continue
        member = members.setdefault(identifier, {
            'member_id': identifier, 'member_name': row.get('member_name'),
            'membership_status': row.get('membership_status'),
            **({'leadership': leadership} if leadership else {}), 'account_tags': []})
        tags = ([row['player_tag']] if row.get('player_tag') else row.get('account_tags', []))
        member['account_tags'] = sorted(set(member['account_tags']) | set(tags))
    return [members[identifier] for identifier in sorted(members)]

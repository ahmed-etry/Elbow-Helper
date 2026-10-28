"""Public owner context for account-level observations and recommendations."""

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.roles import (
    CORE, LEAD, CO_APPLICANT_ROLE_ID, SLEEPING_CO_ROLE_ID, SLEEPING_CROSS_ROLE_ID,
)


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

"""Resolve configured clan role IDs to current guild names without prose matching."""

from elbow_helper.configuration.clans import CLANS


ROLE_PURPOSES = (
    'member_role_id', 'war_role_id', 'cwl_role_id', 'leadership_role_id',
    'cwl_helper_role_id', 'cwl_bench_role_id',
)


def clan_role_purposes(role_id):
    return [{'clan_code': clan.code, 'purpose': field.removesuffix('_role_id')}
            for clan in CLANS.values() for field in ROLE_PURPOSES
            if role_id is not None and getattr(clan, field) == role_id]


def configured_clan_roles(guild, code):
    roles = {role.id: role for role in getattr(guild, 'roles', ())}
    return [{'role_id': role_id, 'name': getattr(roles.get(role_id), 'name', None),
             'purpose': field.removesuffix('_role_id'),
             'status': 'resolved' if role_id in roles else 'unavailable'}
            for field in ROLE_PURPOSES if (role_id := getattr(CLANS[code], field)) is not None]

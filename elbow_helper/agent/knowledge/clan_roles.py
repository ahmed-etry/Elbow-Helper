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



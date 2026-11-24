"""Command access from the help catalogue, expressed as Discord roles."""

from elbow_helper.configuration.roles import implied_role_ids
from elbow_helper.features.help.catalog import HELP_ENTRIES


class MemberCommands:
    def __init__(self, guild_roles, asker_roles=None):
        self.roles = tuple(guild_roles)
        self.entries = tuple(
            entry for entry in HELP_ENTRIES
            if asker_roles is None or entry.visible_to is None or entry.visible_to & asker_roles
        )

    @classmethod
    def for_request(cls, context):
        asker = getattr(context, "member", None)
        return cls(getattr(context.guild, "roles", ()),
                   {role.id for role in getattr(asker, "roles", ())})

    def role_names(self, role_ids):
        return [" ".join(role.name.splitlines()) for role in self.roles if role.id in role_ids]

    def grants(self, held=None):
        """Group help paths by the roles that grant them, or by the member's held grants."""
        grants = {}
        for entry in self.entries:
            roles = entry.visible_to
            if held is not None:
                if not roles or not (roles := roles & held):
                    continue
            grants.setdefault(roles, []).append(entry.path)
        return grants

    def named_grants(self, held):
        return [
            {"through_roles": self.role_names(roles), "paths": paths}
            for roles, paths in self.grants(held).items()
        ]

    def facts(self, roles):
        held = frozenset(roles)
        return {
            "commands": self.named_grants(held),
            # The hierarchy applies to reading access. Command handlers check held roles.
            "additional_roles_for_reading": self.role_names(implied_role_ids(held) - held),
        }

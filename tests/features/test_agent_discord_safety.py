"""Discord action limits hold across target combinations."""

from itertools import product
from types import SimpleNamespace
import unittest

from elbow_helper.features.agent.discord_actions.safety import (
    POWERFUL_PERMISSIONS, DiscordActionRefused, check_member, check_post_access,
    check_role, check_raw_role, check_raw_nickname,
)


class DiscordActionSafetyTests(unittest.TestCase):
    def test_raw_changes_require_requester_permissions(self):
        bot = SimpleNamespace(id=1, top_role=SimpleNamespace(position=30))
        role = SimpleNamespace(id=3, position=1, managed=False, is_default=lambda: False,
                               permissions=SimpleNamespace())
        member = SimpleNamespace(id=6, top_role=SimpleNamespace(position=1))
        for permission in ("manage_roles", "manage_nicknames"):
            setattr(self.requester.guild_permissions, permission, False)
            with self.assertRaises(DiscordActionRefused):
                if permission == "manage_roles":
                    check_raw_role(role, self.guild, bot, {}, requester=self.requester)
                else:
                    check_raw_nickname(member, bot, guild=self.guild, requester=self.requester)
            setattr(self.requester.guild_permissions, permission, True)
            check_role(role, self.guild, bot, {3: "/synthetic"})
            check_member(member, bot)

    def setUp(self):
        self.requester = SimpleNamespace(id=4, top_role=SimpleNamespace(position=10), guild_permissions=SimpleNamespace(manage_roles=True, manage_nicknames=True))
        self.guild = SimpleNamespace(id=2, owner_id=99, get_member=lambda _: self.requester)



    def test_missing_targets_keep_unavailable_errors(self):
        bot = SimpleNamespace(id=1, top_role=SimpleNamespace(position=30))
        with self.assertRaisesRegex(DiscordActionRefused, "That role is unavailable"):
            check_role(None, self.guild, bot, {})
        with self.assertRaisesRegex(DiscordActionRefused, "That member is unavailable"):
            check_member(None, bot)

    def test_every_unsafe_role_combination_is_refused(self):
        bot = SimpleNamespace(id=1, top_role=SimpleNamespace(position=10))
        guild = self.guild
        for default, managed, above, powerful, feature_owned in product((False, True), repeat=5):
            with self.subTest(default=default, managed=managed, above=above,
                              powerful=powerful, feature_owned=feature_owned):
                permissions = SimpleNamespace(**{
                    name: powerful if name == POWERFUL_PERMISSIONS[0] else False
                    for name in POWERFUL_PERMISSIONS
                })
                role = SimpleNamespace(
                    id=2 if default else 3, is_default=lambda: default,
                    managed=managed, position=10 if above else 9,
                    permissions=permissions,
                )
                owners = {role.id: "/synthetic"} if feature_owned else {}
                if any((default, managed, powerful, feature_owned)):
                    with self.assertRaises(DiscordActionRefused):
                        check_raw_role(role, guild, bot, owners, requester=self.requester)
                else:
                    check_raw_role(role, guild, bot, owners, requester=self.requester)

    def test_each_management_permission_refuses_the_role(self):
        bot = SimpleNamespace(id=1, top_role=SimpleNamespace(position=10))
        guild = self.guild
        for selected in POWERFUL_PERMISSIONS:
            with self.subTest(permission=selected):
                role = SimpleNamespace(
                    id=3, is_default=lambda: False, managed=False, position=9,
                    permissions=SimpleNamespace(**{
                        name: name == selected for name in POWERFUL_PERMISSIONS
                    }),
                )
                with self.assertRaises(DiscordActionRefused):
                    check_raw_role(role, guild, bot, {}, requester=self.requester)

    def test_features_allow_high_members_but_refuse_bot(self):
        bot = SimpleNamespace(id=1, top_role=SimpleNamespace(position=10))
        for self_target, above in product((False, True), repeat=2):
            member = SimpleNamespace(
                id=1 if self_target else 3,
                top_role=SimpleNamespace(position=10 if above else 9),
            )
            if self_target:
                with self.assertRaises(DiscordActionRefused):
                    check_member(member, bot)
            else:
                check_member(member, bot)

    def test_both_actors_need_view_and_send_access(self):
        first = SimpleNamespace(id=1)
        second = SimpleNamespace(id=2)
        for asker_view, asker_send, bot_view, bot_send in product((False, True), repeat=4):
            permissions = {
                1: SimpleNamespace(view_channel=asker_view, send_messages=asker_send),
                2: SimpleNamespace(view_channel=bot_view, send_messages=bot_send),
            }
            channel = SimpleNamespace(permissions_for=lambda actor: permissions[actor.id])
            if all((asker_view, asker_send, bot_view, bot_send)):
                check_post_access(channel, first, second)
            else:
                with self.assertRaises(DiscordActionRefused):
                    check_post_access(channel, first, second)

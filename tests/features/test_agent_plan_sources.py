"""Explicit source identifiers stay separate across entity kinds."""

from types import SimpleNamespace
import unittest

from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.agent.plan.sources import (
    requested_channels, requested_clans, requested_member_ids, requested_player_tags,
)


class NamedSourceTests(unittest.TestCase):
    def test_channel_mentions_and_exact_names_resolve_only_matching_channels(self):
        channels = [SimpleNamespace(id=101, name="synthetic-name"),
                    SimpleNamespace(id=202, name="other-name")]
        self.assertEqual(requested_channels("<#101> #SYNTHETIC-NAME", channels), frozenset({101}))
        self.assertEqual(requested_channels("unrelated words", channels), frozenset())

    def test_member_mentions_do_not_become_channel_sources(self):
        self.assertEqual(requested_member_ids("<@101> <@!202> <#303>"), frozenset({101, 202}))
        self.assertEqual(requested_channels("<@101> <@!202>", ()), frozenset())

    def test_every_configured_clan_identifier_stays_out_of_account_sources(self):
        for clan in CLANS.values():
            with self.subTest(code=clan.code):
                self.assertEqual(requested_clans(clan.code), frozenset({clan.code}))
                self.assertEqual(requested_clans(clan.code.lower()), frozenset())
                self.assertEqual(requested_clans(clan.tag), frozenset({clan.code}))
                self.assertEqual(requested_player_tags(clan.tag), frozenset())

    def test_account_tags_are_normalized_without_matching_channel_names(self):
        self.assertEqual(requested_player_tags("#p0 #P0"), frozenset({"#P0"}))
        self.assertEqual(requested_player_tags("#synthetic-name"), frozenset())

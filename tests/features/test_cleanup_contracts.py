"""Synthetic coverage for the small shared feature calculations and pager."""

from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.domain.identity import name_parts, name_segments
from elbow_helper.features.clan_health.commands.player import ClanHealthPlayerCommandMixin, prepare_player_health_window
from elbow_helper.features.cwl.bonus.analysis import bonus_ranking_key
from elbow_helper.features.examination.intake.logic import PROMO_ROUTE_MAP, PROMO_SOURCES, valid_targets_for_source
from elbow_helper.features.examination.intake.view import PromoToClanSelect
from elbow_helper.features.hibernation.config import HIBERNATE_REMOVE_ROLE_IDS, RESTORE_ROLE_IDS, SNAPSHOT_ONLY_ROLE_IDS
from elbow_helper.features.role_connections import edit
from elbow_helper.infrastructure.discord.pagination import FIRST_PAGE_LABEL, LAST_PAGE_LABEL, NEXT_PAGE_LABEL, PREV_PAGE_LABEL
from elbow_helper.infrastructure.discord.transcript_participants import transcript_participants


class SharedCalculationTests(unittest.TestCase):
    def test_hibernation_role_policies_keep_every_member_and_position(self):
        self.assertEqual(HIBERNATE_REMOVE_ROLE_IDS, frozenset((
            1134808592982212750, 1237851803807387709, 1134543216797036564,
            1251100877251412029, 1237843605696151572,
        )))
        for policy, length, expected in (
            (RESTORE_ROLE_IDS, 28, "9b871e7e47ca6790a26ad5b28e45dc47f04d16914c819ac168dd9f19b4384ac1"),
            (SNAPSHOT_ONLY_ROLE_IDS, 50, "cfa8d4aad745f9afcc0efbe01b22f453c768f4a3782b2df1f5069024a68bdc1a"),
        ):
            self.assertEqual(len(policy), length)
            self.assertEqual(hashlib.sha256(json.dumps(policy).encode()).hexdigest(), expected)

    def test_identity_keeps_unicode_normalization_and_return_types(self):
        for value, expected in (("Ｆｏｏ—Straße 7", ["foo", "strasse", "7"]), ("", []), ("玩家_é", ["玩家", "é"])):
            self.assertEqual(name_segments(value), expected)
            self.assertEqual(name_parts(value), tuple(expected))
        for function in (name_segments, name_parts):
            with self.assertRaises(TypeError):
                function(None)

    def test_every_ladder_route_and_menu_order_remains_the_same(self):
        ladder = ("BEE", "BEC", "BEM", "BE1", "BES", "BE4", "BEH")
        self.assertEqual(PROMO_SOURCES, ladder[:-1])
        self.assertEqual(PROMO_ROUTE_MAP, {code: ladder[i + 1:] for i, code in enumerate(ladder[:-1])})
        self.assertEqual([o.value for o in PromoToClanSelect(register_all=True).options], list(ladder[1:]))
        self.assertEqual(valid_targets_for_source("unknown"), ())
        self.assertEqual([o.value for o in PromoToClanSelect().options], ["BEH"])

    def test_bonus_key_keeps_each_tie_breaker_in_order_and_stable_ties(self):
        base = dict(avg_adjusted_delta=1, total_adjusted_delta=2, total_actual=3, attack_count=4, player_name="B")
        candidates = [base, {**base, "player_name": "a"}, {**base, "attack_count": 5},
                      {**base, "total_actual": 4}, {**base, "total_adjusted_delta": 3},
                      {**base, "avg_adjusted_delta": 2}]
        self.assertEqual(sorted(candidates, key=bonus_ranking_key), candidates[::-1])
        self.assertEqual(bonus_ranking_key(base), bonus_ranking_key({**base, "player_name": "b"}))
        with self.assertRaises(ValueError):
            bonus_ranking_key({**base, "avg_adjusted_delta": "bad"})

    def test_participant_sample_keeps_ties_labels_and_remaining_counts(self):
        authors = {i: SimpleNamespace(id=i, mention=f"<@{i}>", name=f"name{i}", display_name=f"Shown {i}") for i in range(1, 8)}
        messages = [SimpleNamespace(author=authors[i]) for i in (3, 2, 1, 3, 2, 4, 5, 6, 7)]
        top, text = transcript_participants(messages)
        self.assertEqual(top, [(3, 2), (2, 2), (1, 1), (4, 1), (5, 1)])
        self.assertEqual(text, "<@3> - Shown 3 — 2 messages\n<@2> - Shown 2 — 2 messages\n<@1> - Shown 1 — 1 message\n<@4> - Shown 4 — 1 message\n<@5> - Shown 5 — 1 message\n+2 more participants")
        self.assertTrue(transcript_participants(messages[:-1])[1].endswith("+1 more participant"))
        self.assertEqual(transcript_participants([]), ([], "No participants"))
        unnamed = SimpleNamespace(id=8, mention="<@8>", name="Fallback")
        self.assertEqual(transcript_participants([SimpleNamespace(author=unnamed)]),
                         ([(8, 1)], "<@8> - Fallback — 1 message"))


class CleanupWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepared_health_labels_reach_builder_for_every_mode_and_default(self):
        mixin = ClanHealthPlayerCommandMixin()
        mixin.build_player_health_export = AsyncMock(return_value=None)
        for mode, label in (("last_7d", "Last 7 days"), ("last_14d", "Last 14 days"),
                            ("custom", "Custom: 2026-10-01 to 2026-10-08"),
                            ("last_30d", "Last 30 days"), ("unknown", "Last 30 days")):
            with self.subTest(mode=mode):
                result = await mixin.run_player_health_export("#PP29L", mode=mode,
                    date_from="2026-10-01", date_to="2026-10-08")
                self.assertEqual(result.status, "empty")
                self.assertEqual(mixin.build_player_health_export.call_args.kwargs["window_label"], label)
        mixin.build_player_health_export.reset_mock()
        result = await mixin.run_player_health_export("#PP29L", mode="custom")
        self.assertEqual(result.status, "invalid_window")
        mixin.build_player_health_export.assert_not_awaited()
        now = datetime(2026, 11, 7, tzinfo=timezone.utc)
        window, error = prepare_player_health_window("unknown", now=now)
        self.assertIsNone(error)
        self.assertEqual((window.end - window.start).days, 30)

    async def test_connection_pagers_keep_controls_clamping_navigation_and_timeouts(self):
        channel = SimpleNamespace(guild=SimpleNamespace(get_role=lambda _: None))
        for cls, selector, title in ((edit.RemoveConnectionView, edit.RemoveConnectionSelect, "Remove Connection"),
                                     (edit.EditConnectionView, edit.EditConnectionSelect, "Edit Connection")):
            for count in (0, 1, 75, 76):
                with self.subTest(view=cls.__name__, count=count):
                    cog = SimpleNamespace(state={"connections": [
                        {"id": str(i), "target_role_id": i + 1, "all": [], "any": []} for i in range(count)]})
                    view = cls(cog, channel, page=-10)
                    self.assertEqual(view.page, 0)
                    self.assertEqual(view.timeout, 300)
                    self.assertEqual(view.build_embed().title, title)
                    buttons = [child.label for child in view.children if hasattr(child, "label")]
                    expected = [PREV_PAGE_LABEL, NEXT_PAGE_LABEL, "Cancel"]
                    if count > 75:
                        expected = [FIRST_PAGE_LABEL, PREV_PAGE_LABEL, NEXT_PAGE_LABEL, LAST_PAGE_LABEL, "Cancel"]
                    self.assertEqual(buttons, expected)
                    if count:
                        self.assertIsInstance(view.children[0], selector)
                        self.assertIs(view.children[0]._parent_view, view)
                    if count > 25:
                        with patch.object(edit, "edit_bound_view", new_callable=AsyncMock) as update:
                            await view.last_page(SimpleNamespace())
                        refreshed = update.call_args.kwargs["view"]
                        self.assertIsInstance(refreshed, cls)
                        self.assertEqual(refreshed.page, view.total_pages - 1)
                        self.assertTrue(refreshed.next_button.disabled)
                        self.assertTrue(view.is_finished())
                        self.assertEqual(refreshed.build_embed().footer.text, f"Page {refreshed.total_pages}/{refreshed.total_pages}")
                    bound = SimpleNamespace(edit=AsyncMock())
                    view.bind_message(bound)
                    await view.on_timeout()
                    self.assertTrue(all(child.disabled for child in view.children))
                    bound.edit.assert_awaited_once_with(view=view)

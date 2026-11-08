"""Agent validation uses values a member can say without opening a panel."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from elbow_helper.agent.features.examination.examiner_profile import prepare_examiner_profile
from elbow_helper.features.examination.panel import ExaminationPanelMixin


class ExaminerValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_feature_choice_errors_identify_the_field_and_choices(self):
        member = SimpleNamespace(id=1)
        for field, value, expected in (("th_levels", [99], "TH11"),
                                       ("timezone", "invalid", "UTC"),
                                       ("status", "invalid", "Active")):
            with self.subTest(field=field):
                with self.assertRaises(ValueError) as raised:
                    ExaminationPanelMixin.prepare_examiner_profile_change(None, member, {field: value})
                self.assertEqual(raised.exception.field, field)
                self.assertIn(expected, raised.exception.choices)

    async def test_unrelated_error_text_is_not_treated_as_a_field_choice(self):
        message = "Synthetic panel list failure"
        workflow = SimpleNamespace(
            examiner_profile_snapshot=lambda _: {}, has_examiner_profile=lambda _: False,
            prepare_examiner_profile_change=lambda *_: (_ for _ in ()).throw(ValueError(message)),
        )
        with patch("elbow_helper.agent.features.examination.examiner_profile._workflow",
                   return_value=(workflow, SimpleNamespace(mention="#exam"))):
            result = await prepare_examiner_profile(SimpleNamespace(member=SimpleNamespace(id=1)), {})
        self.assertEqual(result["issue"], message)

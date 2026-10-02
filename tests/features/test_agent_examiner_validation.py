"""Agent validation uses values a member can say without opening a panel."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from elbow_helper.features.agent.capabilities.examination.examiner_profile import prepare_examiner_profile


class ExaminerValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_panel_choices_name_valid_values(self):
        context = SimpleNamespace(member=SimpleNamespace(id=1))
        channel = SimpleNamespace(mention="#exam")
        for message, values, expected in (
            ("Choose Town Hall coverage from the panel list.",
             {"th_levels": [99]}, "TH11"),
            ("Choose a timezone from the panel list.",
             {"timezone": "invalid"}, "Timezone is invalid"),
        ):
            with self.subTest(message=message):
                workflow = SimpleNamespace(
                    examiner_profile_snapshot=lambda _: {},
                    has_examiner_profile=lambda _: False,
                    prepare_examiner_profile_change=lambda *_: (_ for _ in ()).throw(ValueError(message)),
                )
                with patch("elbow_helper.features.agent.capabilities.examination.examiner_profile._workflow",
                           return_value=(workflow, channel)):
                    result = await prepare_examiner_profile(context, values)
                self.assertEqual(result["status"], "needs_input")
                self.assertIn(expected, result["issue"])
                self.assertNotIn("panel", result["issue"].lower())


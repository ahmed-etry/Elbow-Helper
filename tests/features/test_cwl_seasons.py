"""One league a month: its key is the month; keys stored with a suffix stay readable as history.

Keys of weeks in the family record are covered in ``test_cwl_record``. The old
clan health file's key migration went with clan health's last use of that file.
"""

import re
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.domain import cwl
from elbow_helper.domain.cwl import SEASON_KEY
from elbow_helper.features.cwl.bonus.commands import CwlBonusMixin


class CwlSeasonTests(unittest.IsolatedAsyncioTestCase):
    def test_season_keys_are_a_month_or_a_key_stored_with_a_suffix(self):
        for key in ("2026-10", "2026-06-catchup"):
            self.assertIsNotNone(re.fullmatch(SEASON_KEY, key), key)
        for key in ("2026-13", "2026-6", "2026-06-", "2026-06-Catchup", "2026-06 catchup", "June"):
            self.assertIsNone(re.fullmatch(SEASON_KEY, key), key)
        self.assertFalse(hasattr(cwl, "cwl_season_key"))

    async def test_bonus_season_choices_match_the_typed_month(self):
        workflow = CwlBonusMixin()
        workflow.bonus_reports = SimpleNamespace(available_seasons=AsyncMock(
            return_value=["2026-10", "2026-06-catchup", "2026-06"]))
        choices = await workflow.cwl_bonus_season_autocomplete(
            SimpleNamespace(namespace=SimpleNamespace(clan="BE1")), "2026-10",
        )
        self.assertEqual([choice.value for choice in choices], ["2026-10"])


if __name__ == "__main__":
    unittest.main()

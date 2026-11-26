import unittest
from datetime import datetime, timezone

from elbow_helper.features.member_lifecycle.helpers import account_age


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class AccountAgeTests(unittest.TestCase):
    def test_age_uses_the_two_largest_units(self):
        now = utc(2026, 11, 25, 12)
        self.assertEqual(account_age(utc(2023, 9, 10), now), "3 years, 2 months")
        self.assertEqual(account_age(utc(2026, 8, 20), now), "3 months, 5 days")
        self.assertEqual(account_age(utc(2026, 11, 20), now), "5 days")
        self.assertEqual(account_age(utc(2025, 11, 25), now), "1 year")
        self.assertEqual(account_age(utc(2026, 11, 25), now), "Under a day")

    def test_month_end_creation_dates_do_not_overshoot(self):
        self.assertEqual(account_age(utc(2026, 1, 31), utc(2026, 3, 1)), "1 month, 1 day")


if __name__ == "__main__":
    unittest.main()

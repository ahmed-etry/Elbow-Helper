"""The leave post's role lines, reason and ticket logs stay readable and within Discord's limits."""

from types import SimpleNamespace
import unittest

from elbow_helper.features.member_lifecycle.helpers import reason_value, role_lines, ticket_log_value


class LeavePostLayoutTests(unittest.TestCase):
    def test_role_lines_hold_thirty_visible_characters(self):
        def role(role_id, length):
            return SimpleNamespace(id=role_id, name="r" * length)

        # "@" + 14, a space, "@" + 14 is 31: wraps. 14 and 13 is exactly 30: fits.
        self.assertEqual(role_lines([role(1, 14), role(2, 14)]), "<@&1>\n<@&2>")
        self.assertEqual(role_lines([role(1, 14), role(2, 13)]), "<@&1> <@&2>")
        self.assertEqual(role_lines([role(1, 40), role(2, 3)]), "<@&1>\n<@&2>")

    def test_reason_wraps_between_words(self):
        self.assertEqual(reason_value("🍃", "Ran it back to the lobby", "<b>"), "🍃 Ran it back to the lobby")
        self.assertEqual(reason_value("🥾", "Kicked by " + "n" * 40, "<b>"), "🥾 Kicked by\n<b> " + "n" * 40)

    def test_ticket_logs_that_do_not_fit_are_counted(self):
        links = [f"https://discord.com/channels/{10**18}/{10**18}/{10**18 + n}" for n in range(12)]
        blank = "<:blank:1575301816185389206>"
        value = ticket_log_value(links, blank)
        shown = value.count("[Jump!")
        self.assertLessEqual(len(value), 1024)
        self.assertEqual(value.count("https://"), shown)
        self.assertTrue(value.endswith(f"\n{blank} +{len(links) - shown} more ticket logs"))
        self.assertEqual(ticket_log_value(links[:1], blank), f"📁 [Jump!]({links[0]})")


if __name__ == "__main__":
    unittest.main()

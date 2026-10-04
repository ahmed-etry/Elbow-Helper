"""Role account report pages fit typical role sizes in one read."""

import unittest

from elbow_helper.features.agent.capabilities.account_links.role_audit import role_tools


class RoleReportPageLimitTests(unittest.TestCase):
    def test_report_reader_returns_up_to_fifty_members_per_page(self):
        tools = {tool.definition.name: tool for tool in role_tools()}
        parameters = tools["read_role_account_report"].definition.parameters
        self.assertEqual(parameters["properties"]["limit"]["maximum"], 50)

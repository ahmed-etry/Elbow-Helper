"""Compact tool contracts preserve usable argument and workflow guidance."""

import unittest

from elbow_helper.features.agent.datasets.tools import dataset_tools
from elbow_helper.features.agent.datasets.clash import clash_tools
from elbow_helper.features.agent.capabilities.examination.promotion_route import promotion_route_tools
from elbow_helper.features.agent.knowledge.tools import knowledge_tools
from elbow_helper.features.agent.plan.format import capability_list


class ReadContractGuidanceTests(unittest.TestCase):
    def test_named_bindings_are_visible_in_the_actual_catalogue(self):
        tools = dataset_tools()
        catalogue = capability_list({tool.definition.name: tool for tool in tools})
        self.assertIn("params:{*:value}", catalogue)
        self.assertIn(":name, params:{name:value}", catalogue)

    def test_discovery_and_workflow_requirements_fit_the_catalogue(self):
        tools = (*clash_tools(), *promotion_route_tools(), *knowledge_tools())
        catalogue = capability_list({tool.definition.name: tool for tool in tools})
        self.assertIn("resolved tags, never placeholder values", catalogue)
        self.assertIn("Requires an active routing review", catalogue)
        self.assertIn("historical examination status", catalogue)
        self.assertIn("Open indexed headings for community questions", catalogue)
        self.assertIn("Filenames restrict query", catalogue)

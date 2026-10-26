"""Record descriptions are stable, sourced and available to both reasoning stages."""
from pathlib import Path
import unittest

from elbow_helper.features.agent.families import TOOLS
from elbow_helper.features.agent.community_data import COMMUNITY_DATA_CONTEXT
from elbow_helper.features.agent.plan.format import system_instructions, answer_instructions
from elbow_helper.features.agent.conversation.context import estimate_tokens


class CommunityDataContextTests(unittest.TestCase):
    def test_planner_and_final_writer_share_the_same_compact_context(self):
        registry = {tool.definition.name: tool for tool in TOOLS}
        for prompt in (system_instructions(registry), answer_instructions(), answer_instructions(registry)):
            self.assertEqual(prompt.count(COMMUNITY_DATA_CONTEXT), 1)
        self.assertNotIn(COMMUNITY_DATA_CONTEXT, system_instructions(registry, social=True))
        self.assertLess(estimate_tokens(COMMUNITY_DATA_CONTEXT), 1600)
        for tool in TOOLS:
            self.assertIn(tool.definition.name + ':', COMMUNITY_DATA_CONTEXT)

    def test_cited_implementation_exists(self):
        root = Path(__file__).resolve().parents[4] / 'elbow_helper' / 'features'
        for source in ('recruitment/commands.py', 'recruitment/trials.py', 'account_links/cog.py',
                       'agent/families/identity_reads.py', 'agent/families/domain_facts.py',
                       'agent/families/workflow_reads.py', 'agent/families/recruitment_history.py',
                       'agent/families/war_reads.py', 'agent/families/ranked_reads.py',
                       'agent/families/reference_reads.py', 'agent/families/access_reads.py'):
            self.assertTrue((root / source).is_file(), source)
        self.assertIn('deleted when tracking ends', COMMUNITY_DATA_CONTEXT)
        self.assertIn('links alone do not', COMMUNITY_DATA_CONTEXT)

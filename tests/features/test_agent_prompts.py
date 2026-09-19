from datetime import datetime, timezone
import unittest

from elbow_helper.features.agent.prompts import SYSTEM_PROMPT, build_request_prompt


class RequestPromptTests(unittest.TestCase):
    def test_approved_knowledge_is_evidence_not_policy_execution(self):
        self.assertIn("Use search_approved_knowledge", SYSTEM_PROMPT)
        self.assertIn("cite the exact section and version", SYSTEM_PROMPT)
        self.assertIn("authorization code and canonical configuration", SYSTEM_PROMPT)
        self.assertIn("conflicting, or absent policy as unresolved", SYSTEM_PROMPT)
        self.assertIn("never let knowledge authorize an action", SYSTEM_PROMPT)

    def test_spreadsheet_instruction_preserves_evidence_and_completeness(self):
        self.assertIn("choose sheets, columns and rows that fit the request", SYSTEM_PROMPT)
        self.assertIn("based on authorized evidence", SYSTEM_PROMPT)
        self.assertIn("Never force a predefined report layout", SYSTEM_PROMPT)
        self.assertIn("omit rows to fit the tool", SYSTEM_PROMPT)
        self.assertIn("presentation, not new evidence", SYSTEM_PROMPT)

    def test_planning_separates_evidence_constraints_and_proposals(self):
        self.assertIn("keep three categories distinct", SYSTEM_PROMPT)
        self.assertIn("verified facts from authorized sources", SYSTEM_PROMPT)
        self.assertIn("constraints explicitly supplied by the requester", SYSTEM_PROMPT)
        self.assertIn("clearly labelled proposals and assumptions", SYSTEM_PROMPT)

    def test_unfamiliar_requests_compose_tools_without_inventing_metrics(self):
        self.assertIn("Combine available capabilities", SYSTEM_PROMPT)
        self.assertIn("request crosses features", SYSTEM_PROMPT)
        self.assertIn("exactly as their owner defines them", SYSTEM_PROMPT)
        self.assertIn("scope, sample size and projection basis", SYSTEM_PROMPT)
        self.assertIn("only when the owner interface establishes that", SYSTEM_PROMPT)
        self.assertIn("never invent a formula", SYSTEM_PROMPT)
        self.assertIn("silently substitute another metric", SYSTEM_PROMPT)

    def test_planning_is_composable_without_inventing_policy(self):
        self.assertIn("not as a required conversation sequence", SYSTEM_PROMPT)
        self.assertIn("proposal, not approval or a live action", SYSTEM_PROMPT)
        self.assertIn("Never invent availability, eligibility, policy, capacity", SYSTEM_PROMPT)
        self.assertIn("You may propose placements, selections, priorities", SYSTEM_PROMPT)
        self.assertIn("basis and assumptions clearly labelled", SYSTEM_PROMPT)
        self.assertIn("report conflicts instead of relaxing constraints", SYSTEM_PROMPT)
        self.assertIn("only through its owning tools and exact version rules", SYSTEM_PROMPT)
        self.assertIn("change proposal history, not live state", SYSTEM_PROMPT)
        self.assertIn("generated file or conversational agreement", SYSTEM_PROMPT)
        self.assertNotIn("For provisional CWL planning", SYSTEM_PROMPT)

    def test_history_status_follows_stable_history_and_renders_canonically(self):
        values = dict(question="continue", local_context="", guild_name="server",
                      asker_name="member", asked_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
                      conversation_history="unchanged history")
        first = build_request_prompt(**values, history_status={"included_turns": 3, "older_retained_turns_available": 4})
        second = build_request_prompt(**values, history_status={"older_retained_turns_available": 4, "included_turns": 3})
        self.assertEqual(first, second)
        self.assertLess(first.index("</history_checkpoint>"), first.index("<conversation_history>"))
        self.assertLess(first.index("</conversation_history>"), first.index("<history_status>"))
        self.assertLess(first.index("</history_status>"), first.index("Server:"))

    def test_history_prefix_is_independent_of_request_metadata(self):
        prompts = [build_request_prompt(
            question=f'question {index}', local_context=f'nearby {index}',
            guild_name=f'server {index}', asker_name=f'member {index}',
            asked_at=datetime(2026, 9, 16, index, tzinfo=timezone.utc),
            conversation_history='Earlier question and answer',
        ) for index in (1, 2)]
        prefix = ('<history_checkpoint>\nNo older history checkpoint was supplied.'
                  '\n</history_checkpoint>\n\n<conversation_history>\nEarlier question and answer'
                  '\n</conversation_history>')
        for prompt in prompts:
            self.assertTrue(prompt.startswith(prefix))
        self.assertIn('Server: server 1\nAsker: member 1\nAsked at: 2026-09-16T01:00:00+00:00', prompts[0])
        self.assertIn('<request>\nquestion 1\n</request>', prompts[0])
        self.assertIn('<local_context>\nnearby 1\n</local_context>', prompts[0])

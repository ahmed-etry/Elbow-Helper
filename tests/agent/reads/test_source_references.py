"""Approved facts cite their current owning source without changing revisions."""

import ast
from pathlib import Path
import unittest

from elbow_helper.agent.reads.bot_workflows import bot_workflows
from elbow_helper.agent.reads.domain_facts import APPROVAL, clan_profile, workflow_meanings


class SourceReferenceTests(unittest.TestCase):
    def test_relinked_facts_preserve_approved_revision_and_meanings(self):
        placeholders = clan_profile("BE4")
        self.assertIs(placeholders["has_placeholders"], True)
        self.assertEqual(placeholders["sources"]["has_placeholders"], {
            "source": "elbow_helper/agent/reads/domain_facts.py", "authority": "STRUCTURAL-1-P2",
        })
        self.assertEqual(APPROVAL, "STRUCTURAL-1-P2")
        meanings = {row["event"]: row for row in workflow_meanings()}
        for event in ("trial_ended_followup_sent", "applicant_response_reminder"):
            self.assertEqual(meanings[event]["source"], "elbow_helper/agent/reads/workflow_reads.py")
        self.assertEqual(meanings["trial_ended_followup_sent"]["establishes"], "trial ended and follow-up recorded")
        self.assertEqual(meanings["applicant_response_reminder"]["establishes"], "response follow-up")
        self.assertEqual(meanings["examiner_routed"]["source"], "elbow_helper/features/examination/queries.py")
        self.assertEqual(meanings["examiner_routed"]["does_not_establish"], ["recorded examination response", "promotion decision"])

    def test_workflow_implementation_references_resolve_to_existing_files_and_symbols(self):
        root = Path(__file__).resolve().parents[3]
        rows = bot_workflows()["rows"]
        recruitment = next(row for row in rows if row["workflow"] == "recruitment_history")
        self.assertIn("elbow_helper/features/recruitment/trials.py:complete_trial_end", recruitment["sources"])
        for row in rows:
            for source in row.get("sources", ()):
                if not source.startswith("elbow_helper/"):
                    continue
                filename, _, symbol = source.partition(":")
                path = root / filename
                with self.subTest(source=source):
                    self.assertTrue(path.is_file())
                    if symbol and not symbol.startswith("/"):
                        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
                        names = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
                        names.update(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))
                        self.assertTrue(set(symbol.split(".")) <= names)

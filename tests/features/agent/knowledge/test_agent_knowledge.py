"""Markdown knowledge defaults, access, ranking and prompt bounds."""
from pathlib import Path
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from elbow_helper.features.agent.knowledge.store import KnowledgeStore
from elbow_helper.features.agent.knowledge.tools import search_approved_knowledge
from elbow_helper.features.agent.models import AgentRequestContext, AgentTurnState
from elbow_helper.features.agent.conversation.context import estimate_tokens
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.access import AgentAccessLost, require_evidence_access
from elbow_helper.configuration.roles import AGENT_TESTER_ROLE_ID, LEAD


class KnowledgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.store = KnowledgeStore(self.path)

    def write(self, name, text):
        (self.path / name).write_text(text, encoding="utf-8")

    def test_plain_front_matter_and_old_fields(self):
        self.write("plain.md", "Introduction\n# War\nHit your mirror.\n## Raids\nUse attacks.")
        self.write("old.md", """+++
key="old"
version=7
title="Old title"
topics=["war"]
source_refs=[]
effective_from="2099-01-01"
reviewed_at="2000-01-01"
approved_by=1
visibility="lead"
status="approved"
expires_at="2001-01-01"
+++
# Leads
Review wars.""")
        for status in ("draft", "retired"):
            self.write(status + ".md", f"""+++
status="{status}"
+++
Hidden""")
        sections = self.store.load().active_sections
        self.assertEqual([s.title for s in sections], ["Leads", "plain", "War", "Raids"])
        self.assertEqual(sections[0].visibility, "lead")
        self.assertEqual(sections[1].body, "Introduction")
        self.assertNotIn("Review wars", self.store.load().prompt().text)

    def test_rank_and_reload_interval(self):
        self.write("facts.md", "# War\nOther facts\n# Other\nwar war war")
        with patch("elbow_helper.features.agent.knowledge.store.time.monotonic", return_value=0):
            first = self.store.load()
        self.assertEqual(first.search(first.active_sections, query="war")[0].title, "War")
        self.write("facts.md", "# War\nNew facts")
        with patch("elbow_helper.features.agent.knowledge.store.time.monotonic", return_value=59):
            self.assertIs(self.store.load(), first)
        with patch("elbow_helper.features.agent.knowledge.store.time.monotonic", return_value=60):
            self.assertEqual(self.store.load().active_sections[0].body, "New facts")

    def test_prompt_threshold(self):
        self.write("large.md", "+++\nalways_loaded=true\n+++\n# Facts\n" + "x" * 20000)
        with self.assertLogs("elbow_helper.features.agent.knowledge.store", level="WARNING") as logs:
            self.assertEqual(self.store.load().prompt().core, "")
        self.assertIn("large.md: Facts", " ".join(logs.output))
        with patch(
            "elbow_helper.features.agent.knowledge.store.time.monotonic", return_value=10**12,
        ):
            self.write("large.md", "+++\nalways_loaded=true\n+++\n# Facts\nA fact")
            self.assertIn("A fact", self.store.load().prompt().text)

    def test_core_is_opt_in_approved_visible_and_ordered(self):
        self.write("b.md", "+++\nalways_loaded=true\n+++\n# Second\nSecond body\n# Third\nThird body")
        self.write("a.md", "+++\nalways_loaded=true\n+++\n# First\nFirst body")
        self.write("plain.md", "# Searchable\nSearch-only body")
        for level in ("lead", "lead_plus", "core"):
            self.write(level + ".md", f'+++\nalways_loaded=true\nvisibility="{level}"\n+++\n# Private {level}\nPrivate body')
        self.write("draft.md", '+++\nalways_loaded=true\nstatus="draft"\n+++\n# Draft\nDraft body')
        catalog = self.store.load()
        self.assertEqual(catalog.prompt().core, "### First\nFirst body\n\n### Second\nSecond body\n\n### Third\nThird body")
        for level in ("lead", "lead_plus", "core"):
            prompt = catalog.prompt({level})
            self.assertIn("Private " + level, prompt.text)
            self.assertEqual(prompt.required_access, {level})
        self.assertEqual(catalog.search(catalog.active_sections, query="searchable")[0].body,
                         "Search-only body")

    def test_core_keeps_what_fits_and_skips_oversized_sections(self):
        self.write("core.md", "+++\nalways_loaded=true\n+++\n# First\n" + "a" * 7900
                   + "\n# Oversized\n" + "b" * 8000 + "\n# Last\nSmall body")
        with self.assertLogs("elbow_helper.features.agent.knowledge.store", level="WARNING") as logs:
            prompt = self.store.load().prompt()
        self.assertIn("### First", prompt.core)
        self.assertIn("### Last", prompt.core)
        self.assertNotIn("### Oversized", prompt.core)
        self.assertIn("core.md: Oversized", " ".join(logs.output))
        self.assertLessEqual(estimate_tokens(prompt.core), 4000)

    def test_invalid_always_loaded_does_not_load_file(self):
        self.write("bad.md", '+++\nalways_loaded="true"\n+++\n# Invalid\nBody')
        with self.assertLogs("elbow_helper.features.agent.knowledge.store", level="ERROR"):
            self.assertEqual(self.store.load().active_sections, ())

    def test_topic_index_groups_only_approved_visible_titles_by_file(self):
        self.write("b.md", "# Second topic\nSecond body\n# Third topic\nThird body")
        self.write("a.md", "# First topic\nFirst body")
        self.write("draft.md", '+++\nstatus="draft"\n+++\n# Draft title\nBody')
        for level in ("lead", "lead_plus", "core"):
            self.write(level + ".md", f'+++\nvisibility="{level}"\n+++\n# Restricted {level}\nBody')
        catalog = self.store.load()
        prompt = catalog.prompt()
        self.assertEqual(prompt.core, "")
        self.assertIn("a.md:\n- First topic\nb.md:\n- Second topic\n- Third topic", prompt.index)
        self.assertNotIn("Draft title", prompt.text)
        self.assertNotIn("Restricted", prompt.text)
        self.assertNotIn("lead.md", prompt.text)
        self.assertNotIn("First body", prompt.text)
        self.assertEqual(prompt.required_access, set())
        for level in ("lead", "lead_plus", "core"):
            prompt = catalog.prompt({level})
            self.assertIn("Restricted " + level, prompt.index)
            self.assertEqual(prompt.required_access, {level})
            self.assertLessEqual(estimate_tokens(prompt.index), 2500)

    def test_topic_index_keeps_one_hundred_section_titles(self):
        titles = [f"Community knowledge section title {index}" for index in range(100)]
        self.write("topics.md", "".join(f"# {title}\nBody\n" for title in titles))
        prompt = self.store.load().prompt()
        self.assertFalse(prompt.index_fallback)
        self.assertGreater(estimate_tokens(prompt.index), 1000)
        self.assertLessEqual(estimate_tokens(prompt.index), 2500)
        self.assertEqual(prompt.index.splitlines()[2:], [f"- {title}" for title in titles])

    def test_large_topic_index_falls_back_to_visible_file_counts(self):
        self.write("topics.md", "".join(f"# Topic {index}\nBody\n" for index in range(600)))
        self.write("private.md", '+++\nvisibility="lead"\n+++\n# Restricted title\nBody')
        with self.assertLogs("elbow_helper.features.agent.knowledge.store", level="WARNING") as logs:
            prompt = self.store.load().prompt()
        self.assertEqual(prompt.index.splitlines()[-1], "topics.md: 600 sections")
        self.assertNotIn("Topic 1", prompt.index)
        self.assertNotIn("private.md", prompt.index)
        self.assertIn("using file counts", " ".join(logs.output))
        self.assertLessEqual(estimate_tokens(prompt.index), 2500)

    def test_file_count_fallback_stays_bounded_with_many_files(self):
        for index in range(300):
            self.write(f"topic-{index:03}.md", "# Generic topic\nGeneric body")
        with self.assertLogs("elbow_helper.features.agent.knowledge.store", level="WARNING") as logs:
            prompt = self.store.load().prompt()
        self.assertIn("Additional files omitted", prompt.index)
        self.assertIn("omitted files", " ".join(logs.output))
        self.assertLessEqual(estimate_tokens(prompt.index), 2500)

    def test_search_counts_words_and_repeats_with_title_weight_and_exact_priority(self):
        self.write("topics.md", "# Repeated body\n" + "policy " * 20
                   + "\n# Policy guide\nGeneric body"
                   + "\n# Sparse body\npolicy policy"
                   + "\n# Policies\nGeneric body"
                   + "\n# Policy policy guide\nGeneric body"
                   + "\n# POLICY checklist\npolicy")
        catalog = self.store.load()
        titles = [s.title for s in catalog.search(catalog.active_sections, query="POLICY")]
        self.assertEqual(titles, ["Policies", "Repeated body", "Policy policy guide", "POLICY checklist",
                                  "Policy guide", "Sparse body"])
        self.assertEqual(titles, [s.title for s in catalog.search(catalog.active_sections, query="policies")])

    def test_search_prefers_query_coverage_and_matches_words_not_substrings(self):
        self.write("topics.md", "# Single topic\n" + "war " * 30
                   + "\n# Combined topic\nwar policy"
                   + "\n# Irrelevant topic\nreward policies")
        catalog = self.store.load()
        results = catalog.search(catalog.active_sections, query="wars policies")
        self.assertEqual([s.title for s in results], ["Combined topic", "Single topic", "Irrelevant topic"])
        self.assertEqual([s.title for s in catalog.search(catalog.active_sections, query="WAR")],
                         ["Single topic", "Combined topic"])
        self.assertEqual(catalog.search(catalog.active_sections, query="missing"), ())

    def test_search_normalizes_regular_plurals_and_keeps_stable_ties(self):
        self.write("a.md", "# Generic topic\nbox class match raid status analysis")
        self.write("b.md", "# Another topic\nbox class match raid status analysis")
        catalog = self.store.load()
        for query in ("BOXES", "classes", "matches", "raids", "STATUS", "analysis"):
            self.assertEqual([s.file_name for s in catalog.search(catalog.active_sections, query=query)],
                             ["a.md", "b.md"])
        self.assertEqual(catalog.search(catalog.active_sections, query=""), catalog.active_sections)

    def test_reload_logs_counts_estimates_and_warnings_once(self):
        self.write("core.md", "+++\nalways_loaded=true\n+++\n# Oversized core\n" + "x" * 9000)
        self.write("topics.md", "".join(f"# Generic topic {index}\nBody\n" for index in range(300)))
        self.write("draft.md", '+++\nstatus="draft"\n+++\n# Draft title\nBody')
        self.write("invalid.md", '+++\nalways_loaded="invalid"\n+++\nBody')
        logger = "elbow_helper.features.agent.knowledge.store"
        with (patch(logger + ".time.monotonic", return_value=0),
              self.assertLogs(logger, level="INFO") as logs):
            catalog = self.store.load()
            for levels in ((), {"lead"}, {"core", "lead", "lead_plus"}):
                self.assertIs(catalog.prompt(levels), catalog.prompt(levels))
        info = [line for line in logs.output if line.startswith("INFO:")]
        warnings = [line for line in logs.output if line.startswith("WARNING:")]
        self.assertEqual(len(info), 1)
        self.assertEqual(len(warnings), 2)
        self.assertIn("4 files, 301 sections", info[0])
        self.assertIn("index tokens", info[0])
        self.assertIn("Oversized core", " ".join(warnings))
        self.assertIn("using file counts", " ".join(warnings))
        with self.assertNoLogs(logger, level="INFO"):
            for now in (59, 60, 120):
                with patch(logger + ".time.monotonic", return_value=now):
                    self.assertIs(self.store.load(), catalog)
                    catalog.prompt({"lead"})
        self.write("core.md", "+++\nalways_loaded=true\n+++\n# Small core\nGeneric body")
        with (patch(logger + ".time.monotonic", return_value=180),
              self.assertLogs(logger, level="INFO") as logs):
            reloaded = self.store.load()
        self.assertIsNot(reloaded, catalog)
        self.assertIn("Generic body", reloaded.prompt().core)
        self.assertEqual(sum(line.startswith("INFO:") for line in logs.output), 1)
        self.assertFalse(any("Oversized core" in line for line in logs.output))

    async def test_service_loads_visible_core_and_rechecks_retained_access(self):
        self.write("core.md", '+++\nalways_loaded=true\nvisibility="lead"\n+++\n# Restricted topic\nRestricted body')
        self.write("public.md", "# Search topic\nSearch-only body")
        member = SimpleNamespace(id=1, display_name="Synthetic member", roles=[
            SimpleNamespace(id=AGENT_TESTER_ROLE_ID), SimpleNamespace(id=next(iter(LEAD)))])
        channel = SimpleNamespace(id=2, permissions_for=lambda _: SimpleNamespace(
            view_channel=True, read_message_history=True))
        guild = SimpleNamespace(id=3, name="Synthetic guild", me=member, get_member=lambda _: member)
        source = SimpleNamespace(channel=channel, created_at=datetime.now(timezone.utc))
        requests = []

        def session(**kwargs):
            requests.append(kwargs)
            return SimpleNamespace()

        service = AgentService(SimpleNamespace(create_agent_session=session))
        for allowed in (True, False):
            context = AgentRequestContext(bot=SimpleNamespace(), guild=guild, member=member,
                source_message=source, account_links=None, message_search=None, knowledge_store=self.store)
            service._start_flow(question="Synthetic question", local_context="", context=context,
                                conversation_history="")
            self.assertEqual("Restricted body" in requests[-1]["system_prompt"], allowed)
            self.assertNotIn("Search-only body", requests[-1]["system_prompt"])
            self.assertIn("public.md:\n- Search topic", requests[-1]["system_prompt"])
            self.assertEqual(context.state.required_access, {"lead"} if allowed else set())
            if allowed:
                member.roles.pop()
                with self.assertRaises(AgentAccessLost):
                    await require_evidence_access(context)

    async def test_visibility_and_result_bounds(self):
        self.write("public.md", "".join(
            f"# Public {index}\n" + "x" * 5000 + "\n" for index in range(10)
        ))
        for level in ("lead", "lead_plus", "core"):
            self.write(level + ".md", f"""+++
visibility="{level}"
+++
# Secret {level}
Secret facts""")
        context = SimpleNamespace(
            knowledge_store=self.store, member=SimpleNamespace(id=1),
            guild=object(), state=AgentTurnState(),
        )
        with (
            patch(
                "elbow_helper.features.agent.knowledge.tools.require_evidence_access", AsyncMock(),
            ),
            patch(
                "elbow_helper.features.agent.knowledge.tools.has_access_requirements",
                return_value=False,
            ),
        ):
            result = await search_approved_knowledge(context, {"query": ""})
            self.assertEqual(len(result["sections"]), 8)
            self.assertTrue(all(
                len(s["body"]) <= 4000 and s["visibility"] == "public" for s in result["sections"]
            ))
            self.assertEqual(
                (await search_approved_knowledge(context, {"query": "secret"}))["sections"], [],
            )
        with (
            patch(
                "elbow_helper.features.agent.knowledge.tools.require_evidence_access", AsyncMock(),
            ),
            patch(
                "elbow_helper.features.agent.knowledge.tools.has_access_requirements",
                return_value=True,
            ),
            patch("elbow_helper.features.agent.knowledge.tools.require_lookup_access"),
        ):
            result = await search_approved_knowledge(context, {"query": "secret"})
            self.assertEqual(len(result["sections"]), 3)
            self.assertEqual(context.state.required_access, {"lead", "lead_plus", "core"})

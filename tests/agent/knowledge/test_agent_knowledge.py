"""Markdown knowledge defaults, access, ranking and prompt bounds."""
from pathlib import Path
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from elbow_helper.agent.knowledge.store import (
    KnowledgeCatalog, KnowledgeSection, KnowledgeStore, TOPIC_INDEX_TOKEN_LIMIT,
)
from elbow_helper.agent.knowledge import store as knowledge_store
from elbow_helper.agent.knowledge.tools import channel_knowledge_access, search_approved_knowledge
from elbow_helper.agent.models import AgentRequestContext, AgentTurnState, EvidenceScope
from elbow_helper.agent.conversation.context import estimate_tokens
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.access import AgentAccessLost, require_evidence_access
from elbow_helper.configuration.roles import AGENT_TESTER_ROLE_ID, LEAD
from elbow_helper.agent.plan.results import model_result


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
        self.assertEqual([s.title for s in sections], ["Leads", "plain", "War", "War › Raids"])
        self.assertEqual(sections[0].visibility, "lead")
        self.assertEqual(sections[1].body, "Introduction")
        self.assertNotIn("Review wars", self.store.load().prompt().text)

    def test_rank_and_reload_interval(self):
        self.write("facts.md", "# War\nOther facts\n# Other\nwar war war")
        with patch("elbow_helper.agent.knowledge.store.time.monotonic", return_value=0):
            first = self.store.load()
        self.assertEqual(first.search(first.active_sections, query="war")[0].title, "War")
        self.write("facts.md", "# War\nNew facts")
        with patch("elbow_helper.agent.knowledge.store.time.monotonic", return_value=59):
            self.assertIs(self.store.load(), first)
        with patch("elbow_helper.agent.knowledge.store.time.monotonic", return_value=60):
            self.assertEqual(self.store.load().active_sections[0].body, "New facts")

    def test_prompt_profiles_are_lazy_and_scope_the_index(self):
        self.write("public.md", "# Public topic\nPublic body")
        self.write("private.md", '+++\nvisibility="lead"\nalways_loaded=true\n+++\n# Lead topic\nLead body')
        with patch.object(knowledge_store, "_topic_index", wraps=knowledge_store._topic_index) as index:
            catalog = self.store.load()
            self.assertLessEqual(len(catalog._prompts), 2)
            prompt = catalog.prompt({"lead"})
            self.assertIs(prompt, catalog.prompt({"public", "lead", "unknown"}))
            self.assertIn("Lead body", prompt.core)
            self.assertEqual(prompt.required_access, {"lead"})
            self.assertNotEqual(prompt.index, catalog.prompt().index)
            self.assertIn("Lead topic", prompt.index)
            self.assertEqual(index.call_count, 3)
        self.write("private.md", '+++\nvisibility="lead"\nalways_loaded=true\n+++\n# Lead topic\nChanged body')
        with patch.object(knowledge_store.time, "monotonic", return_value=10**12):
            reloaded = self.store.load()
        self.assertIsNot(reloaded.prompt({"lead"}), prompt)
        self.assertIn("Changed body", reloaded.prompt({"lead"}).core)

    def test_prompt_threshold(self):
        self.write("large.md", "+++\nalways_loaded=true\n+++\n# Facts\n" + "x" * 20000)
        with self.assertLogs("elbow_helper.agent.knowledge.store", level="WARNING") as logs:
            self.assertEqual(self.store.load().prompt().core, "")
        self.assertIn("large.md: Facts", " ".join(logs.output))
        with patch(
            "elbow_helper.agent.knowledge.store.time.monotonic", return_value=10**12,
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
        self.write("core.md", "+++\nalways_loaded=true\n+++\n# First\n" + "a" * 19700
                   + "\n# Oversized\n" + "b" * 20000 + "\n# Last\nSmall body")
        with self.assertLogs("elbow_helper.agent.knowledge.store", level="WARNING") as logs:
            prompt = self.store.load().prompt()
        self.assertIn("### First", prompt.core)
        self.assertIn("### Last", prompt.core)
        self.assertNotIn("### Oversized", prompt.core)
        self.assertIn("core.md: Oversized", " ".join(logs.output))
        self.assertLessEqual(estimate_tokens(prompt.core), 4000)

    def test_invalid_always_loaded_does_not_load_file(self):
        self.write("bad.md", '+++\nalways_loaded="true"\n+++\n# Invalid\nBody')
        with self.assertLogs("elbow_helper.agent.knowledge.store", level="ERROR"):
            self.assertEqual(self.store.load().active_sections, ())

    def test_topic_index_groups_only_public_titles_without_restricted_provenance(self):
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
            self.assertIn(level + ".md", prompt.index)
            self.assertEqual(prompt.required_access, {level})
            self.assertLessEqual(estimate_tokens(prompt.index), TOPIC_INDEX_TOKEN_LIMIT)

    def test_topic_index_keeps_public_titles_across_visibility_profiles(self):
        visibilities = ["public"] * 13 + ["lead", "lead_plus", "core"] * 4
        for file_index, visibility in enumerate(visibilities):
            self.write(f"topics-{file_index:02}.md", f'+++\nvisibility="{visibility}"\n+++\n'
                       + "".join(f"# Topic {file_index:02} section {index:02}\nBody\n"
                                 for index in range(20)))
        catalog = self.store.load()
        self.assertEqual(len(catalog.active_sections), 500)
        self.assertEqual(len(catalog.visible_sections()), 260)
        for levels, prompt in catalog._prompts.items():
            with self.subTest(levels=levels):
                self.assertFalse(prompt.index_fallback)
                self.assertEqual(prompt.index_omitted, ())
                self.assertGreater(prompt.index_tokens, 1500)
                self.assertLessEqual(prompt.index_tokens, TOPIC_INDEX_TOKEN_LIMIT)
                self.assertEqual([line for line in prompt.index.splitlines() if line.startswith("- ")],
                                 [f"- {section.title}" for section in catalog.visible_sections(levels)])
                self.assertEqual(prompt.required_access, {section.visibility for section in catalog.visible_sections(levels)
                                                          if section.visibility != "public"})

    def test_topic_index_uses_full_cap_before_falling_back(self):
        self.assertEqual(TOPIC_INDEX_TOKEN_LIMIT, 14000)
        self.write("topics.md", "# Topic\nBody")
        prompt = self.store.load().prompt()
        title = "Topic" + "x" * (5 * (TOPIC_INDEX_TOKEN_LIMIT - prompt.index_title_tokens))
        self.write("topics.md", f"# {title}\nBody")
        with patch("elbow_helper.agent.knowledge.store.time.monotonic", return_value=10**12):
            prompt = self.store.load().prompt()
        self.assertEqual(prompt.index_tokens, TOPIC_INDEX_TOKEN_LIMIT)
        self.assertFalse(prompt.index_fallback)
        self.assertIn(f"- {title}", prompt.index)
        self.write("topics.md", f"# {title}xxxxx\nBody")
        with (patch("elbow_helper.agent.knowledge.store.time.monotonic", return_value=10**12 + 60),
              self.assertLogs("elbow_helper.agent.knowledge.store", level="WARNING") as logs):
            prompt = self.store.load().prompt()
        self.assertEqual(prompt.index_title_tokens, TOPIC_INDEX_TOKEN_LIMIT + 1)
        self.assertTrue(prompt.index_fallback)
        self.assertEqual(prompt.index.splitlines()[-1], "topics.md: 1 sections")
        self.assertIn("exceeds 14000 tokens", " ".join(logs.output))

    def test_large_topic_index_falls_back_to_visible_file_counts(self):
        self.write("topics.md", "".join(f"# Topic {index}\nBody\n" for index in range(4000)))
        self.write("private.md", '+++\nvisibility="lead"\n+++\n# Restricted title\nBody')
        with self.assertLogs("elbow_helper.agent.knowledge.store", level="WARNING") as logs:
            prompt = self.store.load().prompt()
        self.assertEqual(prompt.index.splitlines()[-1], "topics.md: 4000 sections")
        self.assertNotIn("Topic 1", prompt.index)
        self.assertNotIn("private.md", prompt.index)
        self.assertIn("using file counts", " ".join(logs.output))
        self.assertLessEqual(estimate_tokens(prompt.index), TOPIC_INDEX_TOKEN_LIMIT)

    def test_file_count_fallback_stays_bounded_with_many_files(self):
        for index in range(1200):
            self.write(f"community-knowledge-reference-topic-{index:04}.md",
                       "# Generic topic\nGeneric body")
        with self.assertLogs("elbow_helper.agent.knowledge.store", level="WARNING") as logs:
            prompt = self.store.load().prompt()
        self.assertIn("Additional files omitted", prompt.index)
        self.assertIn("omitted files", " ".join(logs.output))
        self.assertLessEqual(estimate_tokens(prompt.index), TOPIC_INDEX_TOKEN_LIMIT)

    def test_search_weights_titles_and_preserves_exact_priority(self):
        self.write("topics.md", "# Repeated body\n" + "policy " * 20
                   + "\n# Policy guide\nGeneric body"
                   + "\n# Sparse body\npolicy policy"
                   + "\n# Policies\nGeneric body"
                   + "\n# Policy policy guide\nGeneric body"
                   + "\n# POLICY checklist\npolicy")
        catalog = self.store.load()
        titles = [s.title for s in catalog.search(catalog.active_sections, query="POLICY")]
        self.assertEqual(titles[0], "Policies")
        for title in ("Policy guide", "Policy policy guide", "POLICY checklist"):
            self.assertLess(titles.index(title), titles.index("Repeated body"))
        self.assertEqual(titles, [s.title for s in catalog.search(catalog.active_sections, query="policies")])

    def test_search_matches_multiple_terms_and_whole_words_not_substrings(self):
        self.write("topics.md", "# Single topic\n" + "war " * 30
                   + "\n# Combined topic\nwar policy"
                   + "\n# Irrelevant topic\nreward policies")
        catalog = self.store.load()
        results = catalog.search(catalog.active_sections, query="wars policies")
        self.assertEqual([s.title for s in results], ["Combined topic", "Single topic", "Irrelevant topic"])
        self.assertEqual({s.title for s in catalog.search(catalog.active_sections, query="WAR")},
                         {"Single topic", "Combined topic"})
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
        self.write("core.md", "+++\nalways_loaded=true\n+++\n# Oversized core\n" + "x" * 21000)
        self.write("topics.md", "".join(
            f"# Generic community knowledge section for reload logging extended reference {index}\nBody\n"
            for index in range(1000)))
        self.write("draft.md", '+++\nstatus="draft"\n+++\n# Draft title\nBody')
        self.write("invalid.md", '+++\nalways_loaded="invalid"\n+++\nBody')
        logger = "elbow_helper.agent.knowledge.store"
        with (patch(logger + ".time.monotonic", return_value=0),
              self.assertLogs(logger, level="INFO") as logs):
            catalog = self.store.load()
            for levels in ((), {"lead"}, {"core", "lead", "lead_plus"}):
                self.assertIs(catalog.prompt(levels), catalog.prompt(levels))
        info = [line for line in logs.output if line.startswith("INFO:")]
        warnings = [line for line in logs.output if line.startswith("WARNING:")]
        self.assertEqual(len(info), 1)
        self.assertEqual(len(warnings), 2)
        self.assertIn("4 files, 1001 sections", info[0])
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

    async def test_service_loads_visible_index_and_rechecks_retained_access(self):
        self.write("core.md", '+++\nalways_loaded=true\nvisibility="lead"\n+++\n# Restricted topic\nRestricted body')
        self.write("public.md", "# Search topic\nSearch-only body")
        member = SimpleNamespace(id=1, display_name="Synthetic member", roles=[
            SimpleNamespace(id=AGENT_TESTER_ROLE_ID), SimpleNamespace(id=next(iter(LEAD)))])
        channel = SimpleNamespace(id=2, permissions_for=lambda _: SimpleNamespace(
            view_channel=True, read_message_history=True))
        guild = SimpleNamespace(id=3, name="Synthetic guild", me=SimpleNamespace(id=99, bot=True), get_member=lambda _: member,
                                chunked=True, members=[member])
        channel.guild = guild
        source = SimpleNamespace(channel=channel, created_at=datetime.now(timezone.utc))
        requests = []

        def session(**kwargs):
            requests.append(kwargs)
            return SimpleNamespace()

        service = AgentService(SimpleNamespace(create_agent_session=session))
        for allowed in (True, False):
            context = AgentRequestContext(bot=SimpleNamespace(), guild=guild, member=member,
                source_message=source, account_links=None, message_search=None, knowledge_store=self.store)
            context.state.knowledge_channel_access = await channel_knowledge_access(context)
            service._start_flow(question="Analyze the synthetic question", local_context="", context=context,
                                conversation_history="")
            self.assertEqual("Restricted topic" in requests[-1]["prompt"], allowed)
            self.assertNotIn("Restricted body", requests[-1]["system_prompt"])
            self.assertNotIn("Search-only body", requests[-1]["system_prompt"])
            self.assertIn("public.md:\n- Search topic", requests[-1]["prompt"])
            self.assertFalse(context.state.knowledge_core_available)
            self.assertEqual(context.state.required_access, {"lead"} if allowed else set())
            if allowed:
                member.roles.pop()
                with self.assertRaises(AgentAccessLost):
                    await require_evidence_access(context)

    async def test_unused_restricted_titles_do_not_restrict_a_public_answer(self):
        self.write("private.md", '+++\nvisibility="lead"\n+++\n# Restricted topic\nRestricted body')
        self.write("public.md", "# Public topic\nPublic body")
        member = SimpleNamespace(id=1, display_name="Synthetic member", roles=[
            SimpleNamespace(id=AGENT_TESTER_ROLE_ID), SimpleNamespace(id=next(iter(LEAD)))])
        channel = SimpleNamespace(id=2, permissions_for=lambda _: SimpleNamespace(
            view_channel=True, read_message_history=True))
        guild = SimpleNamespace(id=3, name="Synthetic guild", me=member, get_member=lambda _: member)
        model = SimpleNamespace(create_agent_session=Mock(return_value=SimpleNamespace()))
        context = AgentRequestContext(bot=SimpleNamespace(), guild=guild, member=member,
            source_message=SimpleNamespace(channel=channel, created_at=datetime.now(timezone.utc)),
            account_links=None, message_search=None, knowledge_store=self.store)
        AgentService(model)._start_flow(question="Analyze the public question", local_context="",
                                        context=context, conversation_history="")
        self.assertEqual(context.state.required_access, set())
        prompt = model.create_agent_session.call_args.kwargs["prompt"]
        self.assertIn("Public topic", prompt)
        self.assertNotIn("Restricted topic", prompt)
        self.assertNotIn("Restricted body", prompt)
        self.assertNotIn("private.md", prompt)
        # Authorized searches flag matching excluded metadata without exposing it.
        result = await search_approved_knowledge(context, {"query": "Restricted topic", "scope": "requester"})
        self.assertNotIn("Restricted body", str(result))
        self.assertTrue(result["available_privately"])
        self.assertEqual(context.state.required_access, set())
        self.assertFalse(context.state.knowledge_title_access)

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
            guild=SimpleNamespace(id=3, get_member=lambda _: None), state=AgentTurnState(),
        )
        with (
            patch(
                "elbow_helper.agent.knowledge.tools.require_evidence_access", AsyncMock(),
            ),
            patch(
                "elbow_helper.agent.knowledge.tools.request_scope",
                new=AsyncMock(return_value=EvidenceScope(0, frozenset(), frozenset())),
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
                "elbow_helper.agent.knowledge.tools.require_evidence_access", AsyncMock(),
            ),
            patch(
                "elbow_helper.agent.knowledge.tools.has_reader_access_requirements",
                return_value=True,
            ),
            patch("elbow_helper.agent.knowledge.tools.require_lookup_access"),
            patch("elbow_helper.agent.knowledge.tools.request_scope",
                  AsyncMock(return_value=EvidenceScope(0, frozenset(), frozenset({"lead", "lead_plus", "core"})))),
        ):
            result = await search_approved_knowledge(context, {"query": "secret", "scope": "requester"})
            self.assertEqual(len(result["sections"]), 3)
            self.assertEqual(context.state.required_access, {"lead", "lead_plus", "core"})


class KnowledgeCoverageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.store = KnowledgeStore(self.path)
        self.context = SimpleNamespace(knowledge_store=self.store, member=SimpleNamespace(id=1),
                                       guild=SimpleNamespace(id=3, get_member=lambda _: None), state=AgentTurnState())
        for name, replacement in (("require_evidence_access", AsyncMock()),
                                  ("has_reader_access_requirements", lambda *_: False),
                                  ("request_scope", AsyncMock(return_value=EvidenceScope(0, frozenset(), frozenset())))):
            patcher = patch("elbow_helper.agent.knowledge.tools." + name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_unconfigured_empty_and_invisible_matches_are_distinct_without_title_leaks(self):
        self.context.knowledge_store = None
        missing = model_result(await search_approved_knowledge(self.context, {"query": "vault"}))
        self.assertEqual(missing["match_status"], "store_unavailable")
        self.assertEqual(missing["flags"]["status"], "failed")
        self.context.knowledge_store = self.store
        empty = await search_approved_knowledge(self.context, {"query": "vault"})
        self.assertEqual(empty["store_status"], "available")
        self.assertEqual(empty["match_status"], "no_visible_match")
        self.store = KnowledgeStore(self.path)
        self.context.knowledge_store = self.store
        (self.path / "private.md").write_text('+++\nvisibility="core"\n+++\n# Hidden vault title\nVault body', encoding="utf-8")
        hidden = await search_approved_knowledge(self.context, {"query": "vault"})
        self.assertEqual(hidden["match_status"], "no_visible_match")
        exact = await search_approved_knowledge(self.context, {"titles": ["Hidden vault title"]})
        self.assertNotIn("Hidden vault title", str(hidden) + str(exact))
        self.assertNotIn("private.md", str(hidden) + str(exact))

    async def test_long_sections_continue_without_clipping_silently(self):
        body = "x" * 9000
        (self.path / "public.md").write_text("# Long reference\n" + body, encoding="utf-8")
        first = model_result(await search_approved_knowledge(self.context, {"titles": ["Long reference"]}))
        section = first["sections"][0]
        self.assertTrue(section["truncated"])
        self.assertEqual(section["next_section_offset"], 4000)
        self.assertEqual(first["flags"]["status"], "partial")
        second = await search_approved_knowledge(self.context, {"section_id": section["section_id"], "section_offset": 4000})
        last = await search_approved_knowledge(self.context, {"section_id": section["section_id"], "section_offset": 8000})
        self.assertEqual(section["body"] + second["sections"][0]["body"] + last["sections"][0]["body"], body)
        self.assertFalse(last["sections"][0]["truncated"])
        self.assertIsNone(last["sections"][0]["next_section_offset"])

    async def test_non_exhaustive_search_paging_and_partial_store_preserve_limits(self):
        (self.path / "public.md").write_text("".join(f"# Reference {i}\nArchive facts\n" for i in range(10)), encoding="utf-8")
        first = model_result(await search_approved_knowledge(self.context, {"query": "archive", "limit": 8}))
        self.assertFalse(first["search_is_exhaustive"])
        self.assertEqual(first["flags"]["status"], "partial")
        self.assertEqual(first["next_offset"], 8)
        last = await search_approved_knowledge(self.context, {"query": "archive", "offset": 8})
        self.assertEqual(len(last["sections"]), 2)
        self.assertIsNone(last["next_offset"])
        self.assertTrue(first["limitations"]["lexical_search_only"])
        (self.path / "bad.md").write_text('+++\nvisibility="invalid"\n+++\n# Bad\nBody', encoding="utf-8")
        self.context.knowledge_store = KnowledgeStore(self.path)
        with self.assertLogs("elbow_helper.agent.knowledge.store", level="ERROR"):
            partial = await search_approved_knowledge(self.context, {"titles": ["Reference 0"]})
        self.assertEqual(partial["store_status"], "partial")
        self.assertFalse(partial["search_complete"])
        self.assertNotIn("bad.md", str(partial))

    async def test_missing_directory_and_load_failure_report_unavailable(self):
        self.context.knowledge_store = KnowledgeStore(self.path / "missing")
        with self.assertLogs("elbow_helper.agent.knowledge.store", level="ERROR"):
            result = await search_approved_knowledge(self.context, {})
        self.assertEqual(result["store_status"], "unavailable")
        with patch.object(self.context.knowledge_store, "load", side_effect=OSError("synthetic read failure")):
            failed = await search_approved_knowledge(self.context, {})
        self.assertEqual(failed["match_status"], "store_unavailable")


class KnowledgeRankingTests(unittest.TestCase):
    def search(self, sections, query):
        catalog = KnowledgeCatalog("", tuple(KnowledgeSection(*section) for section in sections))
        return [section.title for section in catalog.search(catalog.active_sections, query=query)]

    def test_question_stop_words_do_not_match_or_change_ranking(self):
        sections = [("Broad reference", "what are the rules for how do i " * 50),
                    ("Voucher policy", "Submit a voucher request."),
                    ("Other topic", "voucher " * 20)]
        expected = self.search(sections, "voucher")
        self.assertEqual(self.search(sections, "What are the vouchers for?"), expected)
        self.assertEqual(expected[0], "Voucher policy")
        self.assertNotIn("Broad reference", expected)
        self.assertEqual(self.search(sections, "the and of"), [])

    def test_short_focused_body_beats_long_body_with_equal_term_frequency(self):
        sections = [("Broad reference", "voucher " + "ordinary detail " * 500),
                    ("Focused reference", "voucher")]
        self.assertEqual(self.search(sections, "voucher"), ["Focused reference", "Broad reference"])

    def test_rare_terms_outweigh_repeated_common_terms(self):
        sections = [("Common reference", "token " * 100),
                    ("Specific reference", "archive"),
                    *[(f"Other {i}", "token") for i in range(10)]]
        self.assertEqual(self.search(sections, "token archive")[0], "Specific reference")

    def test_exact_title_keeps_priority_even_for_stop_words_or_a_long_body(self):
        for title in ("Voucher policy", "Who we are"):
            with self.subTest(title=title):
                sections = [("Other reference", title), (title, "ordinary detail " * 500)]
                self.assertEqual(self.search(sections, title)[0], title)

    def test_empty_corpus_and_empty_query_remain_bounded(self):
        self.assertEqual(self.search([], "voucher"), [])
        sections = [(f"Topic {i}", "Body") for i in range(10)]
        self.assertEqual(self.search(sections, ""), [title for title, _ in sections[:8]])

    def test_hidden_sections_do_not_influence_visible_ranking(self):
        public = (KnowledgeSection("First reference", "archive"),
                  KnowledgeSection("Second reference", "voucher"))
        hidden = tuple(KnowledgeSection(f"Hidden {i}", "archive", "core") for i in range(20))
        catalog = KnowledgeCatalog("", public + hidden)
        baseline = KnowledgeCatalog("", public).search(public, query="archive voucher")
        self.assertEqual(catalog.search(catalog.visible_sections(), query="archive voucher"), baseline)

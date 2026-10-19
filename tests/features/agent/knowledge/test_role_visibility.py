"""Department knowledge follows current roles through prompts, reads and delivery."""

from dataclasses import replace
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from elbow_helper.configuration.roles import (
    AGENT_TESTER_ROLE_ID, CORE, CWL_HELPERS, EXAMINERS, LEAD, LEAD_PLUS, RECRUITERS,
)
from elbow_helper.features.agent.access import AgentAccessLost, require_evidence_access
from elbow_helper.features.agent.disclosure import can_show
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.knowledge.store import KnowledgeStore, VISIBILITIES
from elbow_helper.features.agent.knowledge.tools import _section_id, channel_knowledge_access, search_approved_knowledge
from elbow_helper.features.agent.models import AgentRequestContext, AgentTurnState


DEPARTMENTS = {"recruiter": RECRUITERS, "cwl_helper": CWL_HELPERS, "examiner": EXAMINERS}


class RoleVisibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        for level in sorted(VISIBILITIES):
            (self.path / f"{level}.md").write_text(
                f'+++\nvisibility="{level}"\n+++\n# Topic {level}\nReference {level} body',
                encoding="utf-8", newline="\n",
            )
        self.store = KnowledgeStore(self.path)
        self.member = SimpleNamespace(id=1, display_name="Synthetic member", roles=[])
        self.bot_member = SimpleNamespace(id=99, roles=[], bot=True)
        self.members = [self.member, self.bot_member]
        self.guild = SimpleNamespace(
            id=3, name="Synthetic guild", me=self.bot_member, members=self.members, chunked=True,
            get_member=lambda identifier: next((m for m in self.members if m.id == identifier), None),
        )
        self.channel = SimpleNamespace(
            id=2, guild=self.guild,
            permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True),
        )
        self.context = AgentRequestContext(
            bot=SimpleNamespace(), guild=self.guild, member=self.member,
            source_message=SimpleNamespace(channel=self.channel, created_at=datetime.now(timezone.utc)),
            account_links=None, message_search=None, knowledge_store=self.store,
        )
        self.set_roles(())

    def set_roles(self, identifiers):
        self.member.roles = [SimpleNamespace(id=identifier)
                             for identifier in (AGENT_TESTER_ROLE_ID, *identifiers)]
        self.context = replace(self.context, state=AgentTurnState())

    async def test_each_role_sees_only_its_knowledge_and_lead_plus_sees_all_departments(self):
        cases = [((), {"public"})]
        for level, roles in DEPARTMENTS.items():
            cases.extend(((role,), {"public", level}) for role in roles)
        cases.extend(((role,), {"public", "core"}) for role in CORE)
        for role in LEAD_PLUS:
            expected = {"public", "lead_plus", *DEPARTMENTS}
            if role in LEAD:
                expected.add("lead")
            cases.append(((role,), expected))
        for identifiers, expected in cases:
            for arguments in ({}, {"query": "Reference"}):
                with self.subTest(roles=identifiers, arguments=arguments):
                    self.set_roles(identifiers)
                    result = await search_approved_knowledge(self.context, arguments)
                    self.assertEqual({s["visibility"] for s in result["sections"]}, expected)
                    self.assertEqual(self.context.state.required_access, expected - {"public"})

    async def test_multiple_roles_combine_levels_without_granting_other_departments(self):
        self.set_roles((next(iter(RECRUITERS)), next(iter(EXAMINERS)), next(iter(CORE))))
        titles = [f"Topic {level}" for level in ("public", "recruiter", "cwl_helper", "examiner", "core")]
        result = await search_approved_knowledge(self.context, {"titles": titles})
        self.assertEqual([s["visibility"] for s in result["sections"]],
                         ["public", "recruiter", "examiner", "core"])
        self.assertEqual(self.context.state.required_access, {"recruiter", "examiner", "core"})

    async def test_hidden_titles_queries_and_section_ids_match_unknown_results(self):
        self.set_roles((next(iter(RECRUITERS)),))
        for level in ("cwl_helper", "examiner", "lead", "lead_plus", "core"):
            section = next(s for s in self.store.load().active_sections if s.visibility == level)
            for hidden, unknown in (
                ({"titles": [section.title]}, {"titles": ["Unknown"]}),
                ({"query": level}, {"query": "unknown"}),
                ({"section_id": _section_id(section)}, {"section_id": "0" * 64}),
            ):
                with self.subTest(level=level, arguments=hidden):
                    self.assertEqual(await search_approved_knowledge(self.context, hidden),
                                     await search_approved_knowledge(self.context, unknown))
        self.assertEqual(self.context.state.required_access, set())

    async def test_department_role_can_be_replaced_by_lead_plus_before_access_is_lost(self):
        for level, roles in DEPARTMENTS.items():
            with self.subTest(level=level):
                self.set_roles((next(iter(roles)),))
                result = await search_approved_knowledge(self.context, {"titles": [f"Topic {level}"]})
                self.assertEqual(result["sections"][0]["visibility"], level)
                self.member.roles = [SimpleNamespace(id=AGENT_TESTER_ROLE_ID),
                                     SimpleNamespace(id=next(iter(LEAD_PLUS)))]
                await require_evidence_access(self.context)
                self.member.roles.pop()
                with self.assertRaises(AgentAccessLost):
                    await require_evidence_access(self.context)

    def test_all_level_combinations_filter_core_and_keep_discovery_public(self):
        for path in self.path.glob("*.md"):
            text = path.read_text(encoding="utf-8").replace("+++\n", "+++\nalways_loaded=true\n", 1)
            path.write_text(text, encoding="utf-8", newline="\n")
        catalog = self.store.load()
        self.assertEqual(len(catalog.active_sections), 7)
        self.assertLessEqual(len(catalog._prompts), 2)
        levels = sorted(VISIBILITIES - {"public"})
        for count in range(len(levels) + 1):
            for profile in combinations(levels, count):
                with self.subTest(profile=profile):
                    prompt = catalog.prompt(profile)
                    self.assertEqual(prompt.required_access, set(profile))
                    for level in VISIBILITIES:
                        self.assertEqual(f"Reference {level} body" in prompt.core,
                                         level == "public" or level in profile)
                        self.assertEqual(f"Topic {level}" in prompt.index, level == "public")
                    self.assertIs(prompt, catalog.prompt(reversed(profile)))

    async def test_service_prepares_department_prompts_and_retains_each_requirement(self):
        for level in DEPARTMENTS:
            path = self.path / f"{level}.md"
            path.write_text(path.read_text(encoding="utf-8").replace(
                "+++\n", "+++\nalways_loaded=true\n", 1), encoding="utf-8", newline="\n")
        model = SimpleNamespace(create_agent_session=Mock(return_value=SimpleNamespace()))
        service = AgentService(model)
        for identifiers, expected in (
            *((roles, {level}) for level, roles in DEPARTMENTS.items()),
            (LEAD_PLUS, set(DEPARTMENTS)),
            ((next(iter(RECRUITERS)), next(iter(EXAMINERS))), {"recruiter", "examiner"}),
        ):
            with self.subTest(expected=expected):
                self.set_roles(identifiers)
                self.context.state.knowledge_channel_access = await channel_knowledge_access(self.context)
                service._start_flow(question="Explain community processes", local_context="",
                                    context=self.context, conversation_history="")
                prompt = model.create_agent_session.call_args.kwargs["system_prompt"]
                for level in DEPARTMENTS:
                    self.assertEqual(f"Reference {level} body" in prompt, level in expected)
                self.assertEqual(self.context.state.required_access, expected)
                self.member.roles = [SimpleNamespace(id=AGENT_TESTER_ROLE_ID)]
                with self.assertRaises(AgentAccessLost):
                    await require_evidence_access(self.context)

    async def test_channel_audience_requires_every_viewer_to_hold_each_department_level(self):
        for level, roles in DEPARTMENTS.items():
            with self.subTest(level=level):
                self.set_roles((next(iter(roles)),))
                viewer = SimpleNamespace(id=4, roles=[SimpleNamespace(id=next(iter(LEAD_PLUS)))])
                self.members[:] = [self.member, viewer, self.bot_member]
                self.assertTrue(await can_show(self.channel, {}, {level}, self.guild))
                viewer.roles = [SimpleNamespace(id=next(iter(CORE)))]
                self.assertFalse(await can_show(self.channel, {}, {level}, self.guild))
        self.set_roles((next(iter(RECRUITERS)), next(iter(EXAMINERS))))
        viewer.roles = [SimpleNamespace(id=next(iter(RECRUITERS)))]
        self.assertFalse(await can_show(self.channel, {}, {"recruiter", "examiner"}, self.guild))
        viewer.roles.append(SimpleNamespace(id=next(iter(EXAMINERS))))
        self.assertTrue(await can_show(self.channel, {}, {"recruiter", "examiner"}, self.guild))

    async def test_incomplete_audience_cache_uses_department_role_overwrites(self):
        self.guild.chunked = False
        self.guild.owner_id = self.member.id
        default = Mock(id=self.guild.id)
        allowed = set()
        self.channel.overwrites = {}
        self.channel.permissions_for = lambda actor: SimpleNamespace(
            view_channel=actor.id in allowed, read_message_history=actor.id in allowed,
        )
        self.guild.default_role = default
        for level, roles in DEPARTMENTS.items():
            with self.subTest(level=level):
                self.set_roles((next(iter(roles)),))
                department = Mock(id=next(iter(roles)), managed=False)
                lead_plus = Mock(id=next(iter(LEAD_PLUS)), managed=False)
                core = Mock(id=next(iter(CORE)), managed=False)
                self.guild.roles = [default, department, lead_plus, core]
                allowed.clear()
                allowed.update((department.id, lead_plus.id))
                self.assertTrue(await can_show(self.channel, {}, {level}, self.guild))
                allowed.add(core.id)
                self.assertFalse(await can_show(self.channel, {}, {level}, self.guild))

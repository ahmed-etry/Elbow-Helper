from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.core.lifecycle import REQUIRED_EXTENSIONS
from elbow_helper.features.agent import setup


class AgentSetupTests(unittest.IsolatedAsyncioTestCase):
    def test_dependencies_load_before_agent(self):
        agent = REQUIRED_EXTENSIONS.index("elbow_helper.features.agent")
        for name in (
            "wars", "hibernation", "clan_transfers", "account_links",
            "clan_health", "rosters", "cwl", "support_tickets", "recruitment",
            "examination",
            "records", "achievements",
            "event_stats", "member_lifecycle", "clan_reporting",
            "role_connections",
        ):
            self.assertLess(REQUIRED_EXTENSIONS.index(f"elbow_helper.features.{name}"), agent)

    async def test_missing_dependency_does_not_register_partial_agent(self):
        for missing in (
            "AccountLinks", "ClanHealth", "WarManager", "Rosters",
            "CwlManagement", "ClanTransfers",
            "Hibernate", "SupportActions", "Recruitment", "Examination",
            "Records", "Achievements",
            "EventStatsCog",
            "MemberLifecycle",
            "ClanReporting",
            "RoleConnections",
        ):
            with self.subTest(missing=missing):
                dependencies = {
                    "AccountLinks": object(), "ClanHealth": SimpleNamespace(queries=object()),
                    "WarManager": SimpleNamespace(queries=object()),
                    "Rosters": SimpleNamespace(queries=object()),
                    "CwlManagement": SimpleNamespace(queries=object()),
                    "ClanTransfers": SimpleNamespace(queries=object()),
                    "Hibernate": SimpleNamespace(queries=object()),
                    "SupportActions": SimpleNamespace(queries=object()),
                    "Recruitment": SimpleNamespace(queries=object()),
                    "Examination": SimpleNamespace(queries=object()),
                    "Records": SimpleNamespace(queries=object()),
                    "Achievements": SimpleNamespace(queries=object()),
                    "EventStatsCog": SimpleNamespace(queries=object()),
                    "MemberLifecycle": SimpleNamespace(queries=object()),
                    "ClanReporting": SimpleNamespace(queries=object()),
                    "RoleConnections": SimpleNamespace(queries=object()),
                }
                dependencies.pop(missing)
                bot = SimpleNamespace(get_cog=dependencies.get, add_cog=AsyncMock())
                with self.assertRaises(RuntimeError):
                    await setup(bot)
                bot.add_cog.assert_not_awaited()

    async def test_setup_injects_only_feature_read_interfaces(self):
        queries, clan_health_queries, war_queries = object(), object(), object()
        cwl_queries, transfer_queries, hibernation_queries = object(), object(), object()
        support_queries = object()
        recruitment_queries = object()
        examination_queries = object()
        record_queries = object()
        achievement_queries = object()
        event_queries = object()
        member_lifecycle_queries = object()
        clan_reporting_queries = object()
        role_connection_queries = object()
        dependencies = {"AccountLinks": object(), "ClanHealth": SimpleNamespace(queries=clan_health_queries),
                        "WarManager": SimpleNamespace(queries=war_queries),
                        "Rosters": SimpleNamespace(queries=queries),
                        "CwlManagement": SimpleNamespace(queries=cwl_queries),
                        "ClanTransfers": SimpleNamespace(queries=transfer_queries)}
        dependencies["Hibernate"] = SimpleNamespace(queries=hibernation_queries)
        dependencies["SupportActions"] = SimpleNamespace(queries=support_queries)
        dependencies["Recruitment"] = SimpleNamespace(queries=recruitment_queries)
        dependencies["Examination"] = SimpleNamespace(queries=examination_queries)
        dependencies["Records"] = SimpleNamespace(queries=record_queries)
        dependencies["Achievements"] = SimpleNamespace(
            queries=achievement_queries,
        )
        dependencies["EventStatsCog"] = SimpleNamespace(queries=event_queries)
        dependencies["MemberLifecycle"] = SimpleNamespace(
            queries=member_lifecycle_queries,
        )
        dependencies["ClanReporting"] = SimpleNamespace(
            queries=clan_reporting_queries,
        )
        dependencies["RoleConnections"] = SimpleNamespace(
            queries=role_connection_queries,
        )
        bot = SimpleNamespace(get_cog=dependencies.get, add_cog=AsyncMock(), http=object(), paths=SimpleNamespace(data_root=Path("unused")))
        with (patch("elbow_helper.features.agent.CoreAgent") as factory,
              patch("elbow_helper.features.agent.TranscriptArchive") as archive,
              patch("elbow_helper.features.agent.ConversationRepository") as repository,
              patch("elbow_helper.features.agent.ResearchJobRepository") as jobs):
            await setup(bot)
        self.assertIs(factory.call_args.kwargs["transcript_archive"], archive.return_value)
        self.assertIs(factory.call_args.kwargs["persistence"].repository, repository.return_value)
        self.assertIs(factory.call_args.kwargs["research_jobs"], jobs.return_value)
        runner = factory.call_args.kwargs["research_runner"]
        self.assertIs(runner.repository, jobs.return_value)
        self.assertIs(
            runner.message_search, factory.call_args.kwargs["message_search"],
        )
        self.assertIsNotNone(factory.call_args.kwargs["thread_discovery"])
        self.assertIs(factory.call_args.kwargs["roster_queries"], queries)
        self.assertIs(factory.call_args.kwargs["clan_health"], clan_health_queries)
        self.assertIs(factory.call_args.kwargs["cwl_queries"], cwl_queries)
        self.assertIs(factory.call_args.kwargs["war_queries"], war_queries)
        self.assertIs(factory.call_args.kwargs["transfer_queries"], transfer_queries)
        self.assertIs(factory.call_args.kwargs["hibernation_queries"], hibernation_queries)
        self.assertIs(factory.call_args.kwargs["support_queries"], support_queries)
        self.assertIs(factory.call_args.kwargs["recruitment_queries"], recruitment_queries)
        self.assertIs(factory.call_args.kwargs["examination_queries"], examination_queries)
        self.assertIs(factory.call_args.kwargs["record_queries"], record_queries)
        self.assertIs(
            factory.call_args.kwargs["achievement_queries"],
            achievement_queries,
        )
        self.assertIs(factory.call_args.kwargs["event_queries"], event_queries)
        self.assertIs(
            factory.call_args.kwargs["member_lifecycle_queries"],
            member_lifecycle_queries,
        )
        self.assertIs(
            factory.call_args.kwargs["clan_reporting_queries"],
            clan_reporting_queries,
        )
        self.assertIs(
            factory.call_args.kwargs["role_connection_queries"],
            role_connection_queries,
        )
        self.assertEqual(
            factory.call_args.kwargs["knowledge_store"].directory,
            Path("unused") / "agent" / "knowledge",
        )
        self.assertNotIn(dependencies["Rosters"], factory.call_args.kwargs.values())
        self.assertNotIn(dependencies["ClanHealth"], factory.call_args.kwargs.values())
        self.assertNotIn(dependencies["WarManager"], factory.call_args.kwargs.values())
        self.assertNotIn(dependencies["CwlManagement"], factory.call_args.kwargs.values())
        self.assertNotIn(dependencies["ClanTransfers"], factory.call_args.kwargs.values())
        self.assertNotIn(dependencies["Hibernate"], factory.call_args.kwargs.values())
        self.assertNotIn(dependencies["SupportActions"], factory.call_args.kwargs.values())
        self.assertNotIn(dependencies["Recruitment"], factory.call_args.kwargs.values())
        self.assertNotIn(dependencies["Examination"], factory.call_args.kwargs.values())
        self.assertNotIn(dependencies["Records"], factory.call_args.kwargs.values())
        self.assertNotIn(
            dependencies["Achievements"], factory.call_args.kwargs.values(),
        )
        self.assertNotIn(
            dependencies["EventStatsCog"], factory.call_args.kwargs.values(),
        )
        self.assertNotIn(
            dependencies["MemberLifecycle"], factory.call_args.kwargs.values(),
        )
        self.assertNotIn(
            dependencies["ClanReporting"], factory.call_args.kwargs.values(),
        )
        self.assertNotIn(
            dependencies["RoleConnections"], factory.call_args.kwargs.values(),
        )
        bot.add_cog.assert_awaited_once_with(factory.return_value)

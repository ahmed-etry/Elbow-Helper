from __future__ import annotations

import importlib
from pathlib import Path
import unittest

import discord
from discord import app_commands

import elbow_helper.features
from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.core.lifecycle import REQUIRED_EXTENSIONS
from elbow_helper.discord.command_registry import ROOT_NAMES
from elbow_helper.discord.command_registry import setup as setup_command_registry
from elbow_helper.discord.command_registry import teardown as teardown_command_registry
from elbow_helper.features.clan_transfers.cog import ClanTransfers
from elbow_helper.features.cwl.cog import CwlManagement
from elbow_helper.features.diagnostics.cog import DebugCog
from elbow_helper.features.help.catalog import HELP_INDEX
from elbow_helper.features.records.cog import Records
from elbow_helper.features.rosters.cog import Rosters


FEATURE_ROOT = Path(elbow_helper.features.__file__).parent
PROJECT_ROOT = FEATURE_ROOT.parents[1]
CLAN_HEALTH_DATABASE_ROOT = FEATURE_ROOT / "clan_health" / "database"


class _RegistryBot:
    def __init__(self) -> None:
        client = discord.Client(intents=discord.Intents.none())
        self.tree = app_commands.CommandTree(client)
        self._cogs = {
            "CwlManagement": object.__new__(CwlManagement),
            "Rosters": object.__new__(Rosters),
            "ClanTransfers": object.__new__(ClanTransfers),
            "Records": object.__new__(Records),
        }

    def get_cog(self, name: str):
        return self._cogs.get(name)


class CommandRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_roots_build_and_teardown_cleanly(self) -> None:
        bot = _RegistryBot()
        guild = discord.Object(id=GUILD_ID)
        expected = {
            "cwl": ["register", "brief", "bonus", "roster"],
            "roster": [
                "create",
                "edit",
                "timing",
                "schedule",
                "post",
                "export",
                "list",
                "clone",
                "delete",
                "announcement",
            ],
            "transfer": ["request", "cancel", "reminder"],
            "record": ["add", "export", "edit", "remove"],
        }

        for _ in range(2):
            await setup_command_registry(bot)  # type: ignore[arg-type]
            for root, command_names in expected.items():
                group = bot.tree.get_command(root, guild=guild)
                self.assertIsInstance(group, app_commands.Group)
                self.assertEqual(
                    [command.name for command in group.commands],
                    command_names,
                )
            await teardown_command_registry(bot)  # type: ignore[arg-type]
            for root in ROOT_NAMES:
                self.assertIsNone(bot.tree.get_command(root, guild=guild))

    def test_runtime_extension_reload_is_not_exposed(self) -> None:
        command_names = {command.name for command in DebugCog.__cog_app_commands__}

        self.assertNotIn("reload", command_names)
        self.assertNotIn("/reload", HELP_INDEX)


class DependencyBoundaryTests(unittest.TestCase):
    def test_agent_large_module_responsibilities_remain_separated(self) -> None:
        agent_root = FEATURE_ROOT / "agent"
        attachment_tools = (
            agent_root / "tools" / "attachments.py"
        ).read_text(encoding="utf-8")
        storage_codec = (agent_root / "conversation" / "codec.py").read_text(
            encoding="utf-8"
        )
        research_reports = (agent_root / "reports" / "research.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("normalize_cwl_table", attachment_tools)
        self.assertNotIn("CwlImportReport", attachment_tools)
        self.assertIn("decode_report", storage_codec)
        self.assertNotIn("RosterSnapshot(", storage_codec)
        self.assertNotIn("CwlImportData(", storage_codec)
        self.assertNotIn("from .jobs import", research_reports)
        research_runner = (agent_root / "research" / "runner.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(".tools.research", research_runner)
        spreadsheet_tools = (
            agent_root / "tools" / "spreadsheets.py"
        ).read_text(encoding="utf-8")
        export_delivery = (agent_root / "files" / "delivery.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("WorkbookWriter", spreadsheet_tools)
        self.assertNotIn("LocalExportStore", spreadsheet_tools)
        self.assertNotIn("features.cwl", export_delivery)
        self.assertNotIn("RoleAccountReport", export_delivery)
        cwl_tools = (agent_root / "tools" / "cwl.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("roster.analysis", cwl_tools)
        self.assertNotIn("clan_health.database", cwl_tools)
        self.assertNotIn("BonusAnalysisService", cwl_tools)
        cwl_scoring_tools = (
            agent_root / "tools" / "cwl_scoring.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("roster.analysis", cwl_scoring_tools)
        self.assertNotIn("clan_health.database", cwl_scoring_tools)
        self.assertNotIn("BonusAnalysisService", cwl_scoring_tools)
        achievement_tools = (
            agent_root / "tools" / "achievements.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("sqlite3", achievement_tools)
        self.assertNotIn("features.achievements.database", achievement_tools)
        self.assertNotIn("features.achievements.cog", achievement_tools)
        event_tools = (
            agent_root / "tools" / "events.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("features.event_stats.cog", event_tools)
        self.assertNotIn("features.event_stats.state", event_tools)
        self.assertNotIn("save_state", event_tools)
        lifecycle_tools = (
            agent_root / "tools" / "member_lifecycle.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("features.member_lifecycle.cog", lifecycle_tools)
        self.assertNotIn("features.member_lifecycle.state", lifecycle_tools)
        self.assertNotIn("save_state", lifecycle_tools)
        clan_reporting_tools = (
            agent_root / "tools" / "clan_reporting.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("features.clan_reporting.cog", clan_reporting_tools)
        self.assertNotIn("features.account_links", clan_reporting_tools)
        self.assertNotIn("refresh", clan_reporting_tools.split(
            "def clan_reporting_tools", 1,
        )[0])
        knowledge_tools = (
            agent_root / "tools" / "knowledge.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn(".actions", knowledge_tools)
        self.assertNotIn(".action_storage", knowledge_tools)
        self.assertNotIn("attachments", knowledge_tools)
        knowledge_store = (agent_root / "knowledge" / "store.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("discord", knowledge_store)
        self.assertNotIn("Action", knowledge_store)
        action_storage = (agent_root / "actions" / "repository.py").read_text(
            encoding="utf-8"
        )
        action_codec = (
            agent_root / "actions" / "codec.py"
        ).read_text(encoding="utf-8")
        action_integrity = (
            agent_root / "actions" / "integrity.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("json.loads", action_storage)
        self.assertNotIn("sqlite_transaction", action_codec)
        self.assertNotIn(".action_storage import", action_integrity)

    def test_feature_packages_use_standard_entry_files(self) -> None:
        for feature in FEATURE_ROOT.iterdir():
            if not feature.is_dir() or feature.name == "__pycache__":
                continue
            with self.subTest(feature=feature.name):
                self.assertTrue((feature / "__init__.py").is_file())
                self.assertTrue((feature / "cog.py").is_file())
                self.assertFalse((feature / "manager.py").exists())

    def test_every_required_extension_imports(self) -> None:
        for extension in REQUIRED_EXTENSIONS:
            with self.subTest(extension=extension):
                importlib.import_module(extension)

    def test_dependency_order_matches_declared_requirements(self) -> None:
        positions = {
            extension: index
            for index, extension in enumerate(REQUIRED_EXTENSIONS)
        }
        dependencies = {
            "elbow_helper.features.hibernation": ("elbow_helper.features.achievements",),
            "elbow_helper.features.member_lifecycle": (
                "elbow_helper.features.hibernation",
            ),
            "elbow_helper.features.account_links": ("elbow_helper.features.wars",),
            "elbow_helper.features.recruitment": (
                "elbow_helper.features.achievements",
                "elbow_helper.features.event_stats",
                "elbow_helper.features.account_links",
            ),
            "elbow_helper.features.agent": (
                "elbow_helper.features.achievements",
                "elbow_helper.features.hibernation",
                "elbow_helper.features.clan_transfers",
                "elbow_helper.features.support_tickets",
                "elbow_helper.features.recruitment",
                "elbow_helper.features.examination",
                "elbow_helper.features.records",
                "elbow_helper.features.member_lifecycle",
                "elbow_helper.features.clan_reporting",
                "elbow_helper.features.account_links",
                "elbow_helper.features.clan_health",
            ),
            "elbow_helper.features.attack_plans": ("elbow_helper.features.clan_health",),
            "elbow_helper.features.records": ("elbow_helper.features.account_links",),
            "elbow_helper.features.rosters": (
                "elbow_helper.features.account_links",
                "elbow_helper.features.wars",
            ),
            "elbow_helper.features.cwl": (
                "elbow_helper.features.achievements",
                "elbow_helper.features.clan_health",
                "elbow_helper.features.account_links",
                "elbow_helper.features.records",
                "elbow_helper.features.rosters",
            ),
            "elbow_helper.features.clan_reporting": ("elbow_helper.features.account_links",),
            "elbow_helper.discord.command_registry": (
                "elbow_helper.features.clan_transfers",
                "elbow_helper.features.cwl",
                "elbow_helper.features.records",
                "elbow_helper.features.rosters",
            ),
        }
        for consumer, providers in dependencies.items():
            for provider in providers:
                with self.subTest(consumer=consumer, provider=provider):
                    self.assertLess(
                        positions[provider],
                        positions[consumer],
                    )

    def test_feature_modules_do_not_construct_external_ai_clients(self) -> None:
        for path in FEATURE_ROOT.rglob("*.py"):
            source = path.read_text(encoding="utf-8-sig")
            with self.subTest(path=path):
                self.assertNotIn("from openai import", source)
                self.assertNotIn("DeepSeekTextClient(", source)
                self.assertNotIn("OPENAI_API_KEY", source)
                self.assertNotIn("DEEPSEEK_API_KEY", source)
                self.assertNotIn("deepseek-", source)
                self.assertNotIn("gpt-", source)

    def test_feature_modules_do_not_load_process_settings(self) -> None:
        forbidden = ("os.getenv(", "os.environ[", "load_dotenv(")
        for path in FEATURE_ROOT.rglob("*.py"):
            source = path.read_text(encoding="utf-8-sig")
            with self.subTest(path=path):
                for marker in forbidden:
                    self.assertNotIn(marker, source)

    def test_feature_paths_do_not_depend_on_module_depth(self) -> None:
        for path in FEATURE_ROOT.rglob("*.py"):
            source = path.read_text(encoding="utf-8-sig")
            with self.subTest(path=path):
                self.assertNotIn("Path(__file__)", source)

    def test_features_do_not_create_clash_http_sessions(self) -> None:
        for path in FEATURE_ROOT.rglob("*.py"):
            source = path.read_text(encoding="utf-8-sig")
            with self.subTest(path=path):
                self.assertNotIn("aiohttp.ClientSession(", source)

    def test_role_agent_tool_uses_feature_owned_account_refresh(self) -> None:
        source = (
            FEATURE_ROOT / "agent" / "tools" / "roles.py"
        ).read_text(encoding="utf-8-sig")
        self.assertIn("refresh_account_locations", source)
        self.assertNotIn("clash_client", source)
        self.assertIn("compare_reports", source)
        self.assertNotIn("ownership_changed", source)

    def test_thread_discovery_transport_stays_out_of_agent_tools(self) -> None:
        source = (
            FEATURE_ROOT / "agent" / "tools" / "threads.py"
        ).read_text(encoding="utf-8-sig")
        self.assertNotIn("guild.active_threads(", source)
        self.assertNotIn("parent.archived_threads(", source)
        self.assertNotIn("._state.http", source)

    def test_features_use_the_application_owned_export_store(self) -> None:
        offenders: list[str] = []
        for path in FEATURE_ROOT.rglob("*.py"):
            source = path.read_text(encoding="utf-8-sig")
            if "LocalExportStore(" in source:
                offenders.append(str(path))
        self.assertEqual(offenders, [])

    def test_features_schedule_tasks_from_the_running_loop(self) -> None:
        offenders: list[str] = []
        for path in FEATURE_ROOT.rglob("*.py"):
            source = path.read_text(encoding="utf-8-sig")
            if ".bot.loop.create_task(" in source:
                offenders.append(str(path))
        self.assertEqual(offenders, [])

    def test_clan_health_sqlite_contexts_explicitly_close_connections(self) -> None:
        offenders: list[str] = []
        for path in CLAN_HEALTH_DATABASE_ROOT.rglob("*.py"):
            source = path.read_text(encoding="utf-8-sig")
            if "with sqlite3.connect(" in source or "with _connect()" in source:
                offenders.append(str(path))
        self.assertEqual(offenders, [])

    def test_shared_command_roots_have_one_registration_owner(self) -> None:
        offenders: list[str] = []
        for path in FEATURE_ROOT.rglob("*.py"):
            source = path.read_text(encoding="utf-8-sig")
            if any(
                f'Group(name="{root}"' in source
                or f"Group(name='{root}'" in source
                for root in ROOT_NAMES
            ):
                offenders.append(str(path))
        self.assertEqual(offenders, [])

    def test_legacy_top_level_packages_are_gone(self) -> None:
        self.assertFalse((PROJECT_ROOT / "cogs").exists())
        self.assertFalse((PROJECT_ROOT / "config").exists())

    def test_source_has_no_legacy_package_imports(self) -> None:
        offenders: list[str] = []
        for path in (PROJECT_ROOT / "elbow_helper").rglob("*.py"):
            source = path.read_text(encoding="utf-8-sig")
            if (
                "from cogs." in source
                or "from config." in source
                or "import cogs." in source
                or "import config." in source
            ):
                offenders.append(str(path))
        self.assertEqual(offenders, [])

    def test_member_lifecycle_uses_hibernation_contract(self) -> None:
        lifecycle_root = FEATURE_ROOT / "member_lifecycle"
        sources = "\n".join(
            path.read_text(encoding="utf-8-sig")
            for path in lifecycle_root.rglob("*.py")
        )
        self.assertNotIn("data/hibernation/", sources)
        self.assertNotIn("features.hibernation", sources)

    def test_internal_renames_preserve_production_identifiers(self) -> None:
        expected_markers = {
            FEATURE_ROOT / "account_links" / "config.py":
                'Path("data/clan_links/links.sqlite3")',
            FEATURE_ROOT / "account_links" / "views.py":
                'custom_id=f"clan_links:',
            FEATURE_ROOT / "clan_reporting" / "config.py":
                '"data/clan_data/clan_activity.json"',
            FEATURE_ROOT / "clan_reporting" / "views.py":
                'custom_id=f"clan_data:',
            FEATURE_ROOT / "member_lifecycle" / "config.py":
                'Path("data/snapshot_intel/snapshot_intel.json")',
            FEATURE_ROOT / "leadership_news" / "views.py":
                'custom_id="lead_news:',
        }
        for path, marker in expected_markers.items():
            with self.subTest(path=path):
                self.assertIn(
                    marker,
                    path.read_text(encoding="utf-8-sig"),
                )

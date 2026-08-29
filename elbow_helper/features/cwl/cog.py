"""Main CWL management cog shell."""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any
from typing import Dict
from typing import Optional
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from elbow_helper.features.wars.emojis import WarEmojiProvider
from elbow_helper.domain.cwl import is_cwl_window
from elbow_helper.infrastructure.clash import ClashClient
from elbow_helper.infrastructure.exports import GoogleSheetsPublisher
from elbow_helper.infrastructure.exports import WorkbookWriter
from elbow_helper.configuration.roles import CWL_HELPERS
from elbow_helper.configuration.roles import LEAD_PLUS

from .announcements import CwlAnnouncementMixin
from .bonus import CwlBonusDashboardMixin
from .bonus import CwlBonusMixin
from .bonus import BonusAnalysisService
from .bonus import BonusConfigRepository
from .bonus import BonusDashboardStore
from .bonus import BonusReportService
from .bonus import BonusWorkbookWriter
from .config import CLAN_NAME_TO_TAG
from .config import THREAD_CLAN_CONFIGS
from .config import THREAD_STATE_FILE
from .dashboard import CwlDashboardMixin
from .roster import CwlRosterAnalysisMixin
from .roster import CwlRosterExportMixin
from .roster import CwlRosterMixin
from .router import CwlRouterMixin
from .threads import CwlThreadMixin
from .transfer_hub import CwlTransferHubMixin
from .transfers import CwlTransferMixin
from .war_board import CwlWarBoardMixin

if TYPE_CHECKING:
    from elbow_helper.features.achievements.rewards import AchievementRewardService
    from elbow_helper.features.clan_health.database import ClanHealthRepository
    from elbow_helper.features.records.database import RecordReader
    from elbow_helper.features.rosters.services.accounts import AccountLinkSource
    from elbow_helper.features.rosters.services.automation import RosterAutomationService
    from elbow_helper.features.rosters.services.queries import RosterQueries


class CwlManagement(
    CwlThreadMixin,
    CwlAnnouncementMixin,
    CwlTransferHubMixin,
    CwlTransferMixin,
    CwlDashboardMixin,
    CwlBonusDashboardMixin,
    CwlBonusMixin,
    CwlRosterAnalysisMixin,
    CwlRosterExportMixin,
    CwlRosterMixin,
    CwlWarBoardMixin,
    CwlRouterMixin,
    commands.Cog,
):
    """Unified CWL management domain."""

    def __init__(
        self,
        bot: commands.Bot,
        clash_client: ClashClient,
        google_publisher: GoogleSheetsPublisher,
        workbook_writer: WorkbookWriter,
        achievement_rewards: AchievementRewardService,
        clan_health_repository: ClanHealthRepository,
        record_reader: RecordReader,
        account_links: AccountLinkSource,
        roster_queries: RosterQueries,
        roster_automation: RosterAutomationService,
    ):
        self.bot = bot
        self.clash_client = clash_client
        self.google_publisher = google_publisher
        self.workbook_writer = workbook_writer
        self.achievement_rewards = achievement_rewards
        self.clan_health_repository = clan_health_repository
        self.record_reader = record_reader
        self.account_links = account_links
        self.roster_queries = roster_queries
        self.roster_automation = roster_automation
        self.logger = logging.getLogger(__name__)

        self.data_file = str(THREAD_STATE_FILE)
        self.clan_configs = copy.deepcopy(THREAD_CLAN_CONFIGS)
        self.clan_tags = dict(CLAN_NAME_TO_TAG)
        self.lead_role_ids = set(LEAD_PLUS)
        self.helper_role_ids = set(CWL_HELPERS)

        self.init_thread_feature()

        self._sent_keys = set(self._load_sent_keys())
        self.dashboard_state = self._load_dashboard_state()
        self.bonus_config = BonusConfigRepository()
        self.cwl_exports = bot.local_exports
        self.bonus_analysis = BonusAnalysisService(
            clash_client,
            clan_health_repository,
        )
        self.bonus_workbook_writer = BonusWorkbookWriter()
        self.bonus_reports = BonusReportService(
            self.bonus_analysis,
            self.bonus_config,
            self.bonus_workbook_writer,
            google_publisher,
            self.cwl_exports,
        )
        self.bonus_dashboard_store = BonusDashboardStore()
        self.transfer_state = self._load_transfer_state()
        self._transfer_reminder_lock = asyncio.Lock()
        self._transfer_hub_lock = asyncio.Lock()
        if getattr(self, "_transfer_state_needs_save", False):
            self._save_transfer_state()
            self._transfer_state_needs_save = False
        self._hero_sum_cache: Dict[str, Dict[str, Any]] = {}
        self._clan_league_cache: Dict[str, Dict[str, Any]] = {}
        self._manual_dashboard_refresh_locks: Dict[str, asyncio.Lock] = {}
        self._manual_dashboard_refresh_last_ts: Dict[str, float] = {}
        self._dashboard_refresh_warning_state: Dict[str, Dict[str, Any]] = {}
        self.bonus_config.ensure()

        self.state = self._load_state()
        self.cwl_board_registry = self.state.setdefault("cwl_board_messages", {})
        self._cwl_board_locks: Dict[str, asyncio.Lock] = {}
        self.cwl_war_emojis = WarEmojiProvider(bot)
        self._poll_task: Optional[asyncio.Task] = None
        self._leaguegroup_cache: Dict[str, Dict[str, Any]] = {}
        self._war_cache: Dict[str, Dict[str, Any]] = {}

        self.start_thread_tasks()
        self.reminder_loop.start()
        self.dashboard_loop.start()
        self._transfer_hub_task = asyncio.create_task(
            self._bootstrap_transfer_hub()
        )

    def cog_unload(self) -> None:
        self.stop_thread_tasks()
        self.reminder_loop.cancel()
        self.dashboard_loop.cancel()
        if self._transfer_hub_task and not self._transfer_hub_task.done():
            self._transfer_hub_task.cancel()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()

    def _has_any_role(self, interaction: discord.Interaction, role_ids: set[int]) -> bool:
        return any(role.id in role_ids for role in getattr(interaction.user, 'roles', []))

    def _is_cwl_window(self) -> bool:
        return is_cwl_window()

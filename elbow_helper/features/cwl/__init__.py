"""CWL management package."""

from __future__ import annotations

from elbow_helper.core.lifecycle import ElbowHelperBot
from .bonus.dashboard import CwlBonusDashboardView
from .config import DASHBOARD_THREADS
from .cog import CwlManagement
from .views import CwlPrepRefreshView
from .views import CwlTransferHubView
async def setup(bot: ElbowHelperBot) -> None:
    achievements = bot.get_cog("Achievements")
    achievement_rewards = getattr(achievements, "rewards", None)
    if achievement_rewards is None:
        raise RuntimeError(
            "CWL management requires the Achievements reward service"
        )
    clan_health = bot.get_cog("ClanHealth")
    clan_health_repository = getattr(clan_health, "repository", None)
    if clan_health_repository is None:
        raise RuntimeError(
            "CWL management requires Clan Health history"
        )
    records = bot.get_cog("Records")
    record_reader = getattr(records, "reader", None)
    if record_reader is None:
        raise RuntimeError(
            "CWL management requires the Records reader"
        )
    account_links = bot.get_cog("AccountLinks")
    if account_links is None:
        raise RuntimeError(
            "CWL management requires Account Links"
        )
    rosters = bot.get_cog("Rosters")
    if rosters is None:
        raise RuntimeError(
            "CWL management requires Rosters"
        )
    cog = CwlManagement(
        bot,
        bot.clash_client,
        bot.google_publisher,
        bot.workbook_writer,
        achievement_rewards,
        clan_health_repository,
        record_reader,
        account_links,
        rosters.queries,
        rosters.automation,
    )
    await bot.add_cog(cog)
    bot.add_view(CwlBonusDashboardView(cog))
    bot.add_view(CwlTransferHubView(cog, placements_released=False))

    for clan_code in DASHBOARD_THREADS:
        bot.add_view(CwlPrepRefreshView(cog, clan_code))

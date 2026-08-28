"""Native Discord roster extension."""

from __future__ import annotations

from elbow_helper.core.lifecycle import ElbowHelperBot

from .services.accounts import RosterAccountDirectory
from .repository import RosterRepository
from .cog import Rosters
from .services.roles import RosterRoleSynchronizer
async def setup(bot: ElbowHelperBot) -> None:
    account_links = bot.get_cog("AccountLinks")
    if account_links is None:
        raise RuntimeError("Rosters requires the AccountLinks cog")
    war_manager = bot.get_cog("WarManager")
    if war_manager is None:
        raise RuntimeError("Rosters requires the WarManager cog")

    repository = RosterRepository()
    cog = Rosters(
        bot,
        bot.clash_client,
        bot.google_publisher,
        repository,
        RosterAccountDirectory(account_links),
        RosterRoleSynchronizer(
            bot,
            repository,
            war_manager.war_lineup_needs_role,
        ),
    )
    war_manager.set_roster_role_claim(cog.queries.role_has_signup)
    await bot.add_cog(cog)

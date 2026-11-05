from elbow_helper.core.lifecycle import ElbowHelperBot

from .cog import Planning


async def setup(bot: ElbowHelperBot) -> None:
    clan_health = bot.get_cog("ClanHealth")
    directory = getattr(clan_health, "directory", None)
    if directory is None:
        raise RuntimeError(
            "Attack Plans requires Clan Health history"
        )
    profiles = getattr(bot.family_collectors, "profiles", None)
    if profiles is None:
        raise RuntimeError("Attack Plans requires the collectors' profiles")
    await bot.add_cog(
        Planning(bot, profiles, directory)
    )

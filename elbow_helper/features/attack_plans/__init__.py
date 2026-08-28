from elbow_helper.core.lifecycle import ElbowHelperBot

from .cog import Planning


async def setup(bot: ElbowHelperBot) -> None:
    clan_health = bot.get_cog("ClanHealth")
    repository = getattr(clan_health, "repository", None)
    if repository is None:
        raise RuntimeError(
            "Attack Plans requires Clan Health history"
        )
    await bot.add_cog(
        Planning(bot, bot.clash_client, repository)
    )

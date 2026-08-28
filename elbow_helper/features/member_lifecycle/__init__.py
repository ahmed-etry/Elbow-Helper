from discord.ext import commands

from .cog import MemberLifecycle


async def setup(bot: commands.Bot) -> None:
    hibernation = bot.get_cog("Hibernate")
    hibernation_reader = getattr(hibernation, "reader", None)
    if hibernation_reader is None:
        raise RuntimeError("Member Lifecycle requires Hibernation")
    await bot.add_cog(MemberLifecycle(bot, hibernation_reader))


__all__ = ("MemberLifecycle", "setup")

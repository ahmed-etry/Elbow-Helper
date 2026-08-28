from .cog import LeadNews


async def setup(bot):
    await bot.add_cog(LeadNews(bot))

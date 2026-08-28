from elbow_helper.discord.views import TranscriptLinkPromptView

from .ai import SupportWelcomeService
from .cog import SupportActions


async def setup(bot):
    cog = SupportActions(bot, SupportWelcomeService(bot.text_generator))
    await bot.add_cog(cog)
    bot.add_view(cog.build_close_view())
    bot.add_view(cog.build_confirm_view())
    bot.add_view(TranscriptLinkPromptView("support_transcript_link"))

"""Leadership Records Discord adapter composition."""

from __future__ import annotations

from discord.ext import commands

from .commands import RecordCommandMixin
from .database import RecordReader
from .export_service import RecordExportService
from .service import RecordService


class Records(RecordCommandMixin, commands.Cog):
    """Internal leadership incident records."""

    def __init__(
        self,
        bot: commands.Bot,
        service: RecordService,
        exports: RecordExportService,
    ):
        self.bot = bot
        self.service = service
        self.reader: RecordReader = service.reader
        self.exports = exports

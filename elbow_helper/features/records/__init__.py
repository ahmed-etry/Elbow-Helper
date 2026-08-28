"""Leadership Records package."""

from __future__ import annotations

from elbow_helper.core.lifecycle import ElbowHelperBot
from elbow_helper.infrastructure.exports import LocalExportStore

from .config import EXPORT_DIR
from .database import RecordRepository
from .export_service import RecordExportService
from .cog import Records
from .service import RecordService
from .sheets.export import RecordWorkbookWriter


async def setup(bot: ElbowHelperBot) -> None:
    account_links = bot.get_cog("AccountLinks")
    if account_links is None:
        raise RuntimeError("Records requires the Account Links service")
    repository = RecordRepository()
    repository.initialize()
    service = RecordService(repository, account_links)
    exports = RecordExportService(
        service.reader,
        service,
        RecordWorkbookWriter(bot.workbook_writer),
        bot.google_publisher,
        LocalExportStore(EXPORT_DIR),
    )
    await bot.add_cog(Records(bot, service, exports))


__all__ = ["Records", "setup"]

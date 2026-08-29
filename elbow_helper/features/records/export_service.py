"""Leadership record workbook creation and publication workflow."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
import logging
import re
import unicodedata

from elbow_helper.infrastructure.exports import GoogleSheetsPublisher
from elbow_helper.infrastructure.exports import LocalExportStore

from .database import RecordReader
from .service import RecordService
from .sheets.export import RecordWorkbookWriter


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecordExport:
    workbook_path: Path
    workbook_name: str
    google_link: str | None
    google_warning: str | None


def _filename_segment(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return (
        re.sub(r"[^a-z0-9]+", "_", ascii_value.casefold()).strip("_")
        or "member"
    )


class RecordExportService:
    """Create and publish a complete leadership-record export."""

    def __init__(
        self,
        reader: RecordReader,
        records: RecordService,
        writer: RecordWorkbookWriter,
        publisher: GoogleSheetsPublisher,
        exports: LocalExportStore,
    ):
        self._reader = reader
        self._records = records
        self._writer = writer
        self._publisher = publisher
        self._exports = exports

    async def create(
        self,
        *,
        member_id: int | None,
        member_name: str | None,
    ) -> RecordExport:
        deleted, cleanup_warning = await asyncio.to_thread(
            self._exports.cleanup,
            "*.xlsx",
        )
        if deleted:
            LOGGER.info("Deleted %s abandoned local export files", deleted)
        if cleanup_warning:
            LOGGER.warning("Local cleanup warning: %s", cleanup_warning)
        records = await asyncio.to_thread(
            self._reader.list,
            member_id=member_id,
        )
        links = await asyncio.to_thread(
            self._records.links_for,
            records,
        )
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        scope = _filename_segment(member_name) if member_id is not None else "all"
        workbook_name = f"leadership_records_{scope}_{timestamp}.xlsx"
        workbook_path = self._exports.temporary_path("leadership_records")
        await asyncio.to_thread(
            self._writer.write,
            workbook_path,
            records,
            links,
            include_empty_categories=member_id is None,
        )
        title = (
            f"Leadership Records - {member_name}"
            if member_name
            else "Leadership Records"
        )
        google_link, google_warning = await self._publisher.upload_workbook(
            workbook_path,
            title,
        )
        return RecordExport(
            workbook_path=workbook_path,
            workbook_name=workbook_name,
            google_link=google_link,
            google_warning=google_warning,
        )

    async def discard(self, report: RecordExport) -> None:
        warning = await asyncio.to_thread(
            self._exports.delete,
            report.workbook_path,
        )
        if warning:
            LOGGER.warning("Local cleanup warning: %s", warning)

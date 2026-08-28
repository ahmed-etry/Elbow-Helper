"""Leadership record workbook creation and publication workflow."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path

from elbow_helper.infrastructure.exports import GoogleSheetsPublisher
from elbow_helper.infrastructure.exports import LocalExportStore

from .database import RecordReader
from .service import RecordService
from .sheets.export import RecordWorkbookWriter


@dataclass(frozen=True, slots=True)
class RecordExport:
    workbook_path: Path
    workbook_name: str
    google_link: str | None
    google_warning: str | None


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
        records = await asyncio.to_thread(
            self._reader.list,
            member_id=member_id,
        )
        links = await asyncio.to_thread(
            self._records.links_for,
            records,
        )
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        workbook_name = f"leadership_records_{timestamp}.xlsx"
        workbook_path = self._exports.path_for(workbook_name)
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
            cleanup_name_contains="Leadership Records",
            retention_days=0,
        )
        return RecordExport(
            workbook_path=workbook_path,
            workbook_name=workbook_name,
            google_link=google_link,
            google_warning=google_warning,
        )

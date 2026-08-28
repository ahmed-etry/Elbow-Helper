"""Leadership Records repository composition."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from typing import Iterable

from ..config import DB_PATH
from .queries import RecordQueries
from .records import RecordWriter
from .schema import RecordSchema


class RecordRepository(RecordSchema, RecordWriter, RecordQueries):
    """Own all SQL and stored record conversion."""

    def __init__(self, path: Path = DB_PATH):
        self.path = path

    initialize = RecordSchema._init_db
    insert = RecordWriter._insert_record
    update = RecordWriter._update_record
    remove = RecordWriter._remove_record
    list = RecordQueries._load_records


class RecordReader:
    """Read-only Records contract supplied to other features."""

    def __init__(self, repository: RecordRepository):
        self._repository = repository

    def list(
        self,
        *,
        member_id: int | None = None,
        include_removed: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._repository.list(
            member_id=member_id,
            include_removed=include_removed,
            limit=limit,
        )

    def active_for_members(
        self,
        member_ids: Iterable[int],
    ) -> list[dict[str, Any]]:
        return self._repository.active_for_members(member_ids)

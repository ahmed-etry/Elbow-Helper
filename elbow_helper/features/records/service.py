"""Leadership record use cases independent from Discord command wiring."""

from __future__ import annotations

import time
from typing import Any
from typing import Protocol

from .database import RecordReader
from .database import RecordRepository
from .domain.types import category_label
from .domain.types import incident_type_label
from .domain.types import resolve_incident_type


class AccountLinkReader(Protocol):
    def get_links_for_user(
        self,
        discord_user_id: int,
    ) -> list[dict[str, Any]]: ...


class RecordService:
    """Create, edit, remove, and read leadership records."""

    def __init__(
        self,
        repository: RecordRepository,
        account_links: AccountLinkReader,
    ):
        self._repository = repository
        self._account_links = account_links
        self.reader = RecordReader(repository)

    @staticmethod
    def display_name(user: Any) -> str:
        return str(
            getattr(user, "display_name", None)
            or getattr(user, "name", None)
            or user.id
        )

    def create(
        self,
        *,
        member: Any,
        category_key: str,
        incident_type_key: str,
        note: str,
        recorder: Any,
    ) -> dict[str, Any]:
        resolved_type = resolve_incident_type(
            category_key,
            incident_type_key,
        )
        if resolved_type is None:
            raise ValueError(
                "Choose an incident type from the selected category."
            )
        if not note.strip():
            raise ValueError("Add details about what happened.")
        return self._repository.insert(
            created_ts=int(time.time()),
            member_id=member.id,
            member_display=self.display_name(member),
            category_key=category_key,
            incident_type_key=resolved_type,
            note=note.strip(),
            recorder_id=recorder.id,
            recorder_display=self.display_name(recorder),
        )

    def edit(
        self,
        *,
        record_id: int,
        member_id: int,
        category_key: str,
        incident_type_key: str,
        note: str,
        editor: Any,
    ) -> dict[str, Any] | None:
        resolved_type = resolve_incident_type(
            category_key,
            incident_type_key,
        )
        if resolved_type is None:
            raise ValueError(
                "Choose an incident type from the selected category."
            )
        if not note.strip():
            raise ValueError("Add details about what happened.")
        return self._repository.update(
            record_id=record_id,
            member_id=member_id,
            category_key=category_key,
            incident_type_key=resolved_type,
            note=note.strip(),
            updated_ts=int(time.time()),
            edited_by_id=editor.id,
            edited_by_display=self.display_name(editor),
        )

    def remove(
        self,
        *,
        record_id: int,
        member_id: int,
        remover: Any,
    ) -> dict[str, Any] | None:
        return self._repository.remove(
            record_id=record_id,
            member_id=member_id,
            removed_ts=int(time.time()),
            removed_by_id=remover.id,
            removed_by_display=self.display_name(remover),
        )

    @staticmethod
    def confirmation(record: dict[str, Any]) -> str:
        return (
            "Recorded "
            f"{category_label(str(record.get('category_key') or ''))} - "
            f"{incident_type_label(str(record.get('incident_type_key') or ''))} "
            f"for {record.get('member_display') or record.get('member_id')}."
        )

    def links_for(
        self,
        records: list[dict[str, Any]],
    ) -> dict[int, list[dict[str, Any]]]:
        return {
            member_id: self._account_links.get_links_for_user(member_id)
            for member_id in {
                int(record.get("member_id") or 0)
                for record in records
            }
            if member_id > 0
        }

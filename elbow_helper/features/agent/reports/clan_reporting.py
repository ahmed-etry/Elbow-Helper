"""Restart-persistent Missing Elder evidence."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from typing import Any

from elbow_helper.configuration.channels import CLAN_LEADERSHIP_CHANNELS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.features.clan_reporting.queries import MissingElderSnapshot


@dataclass(frozen=True, slots=True)
class MissingElderReport:
    report_id: str
    guild_id: int
    source_channel_ids: tuple[int, ...]
    omitted_inaccessible_clan_codes: tuple[str, ...]
    snapshot: MissingElderSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        rows = self.snapshot.rows
        expected_sources = tuple(sorted(
            CLAN_LEADERSHIP_CHANNELS[code]
            for code in self.snapshot.selected_clan_codes
        ))
        expected_omitted = tuple(
            code for code in CLAN_LEADERSHIP_CHANNELS
            if code not in self.snapshot.selected_clan_codes
            and code in self.omitted_inaccessible_clan_codes
        )
        if (
            not isinstance(self.report_id, str) or not self.report_id
            or len(self.report_id) > 32
            or type(self.guild_id) is not int or self.guild_id <= 0
            or self.source_channel_ids != expected_sources
            or len(set(self.source_channel_ids)) != len(self.source_channel_ids)
            or self.omitted_inaccessible_clan_codes != expected_omitted
            or set(self.omitted_inaccessible_clan_codes)
                & set(self.snapshot.selected_clan_codes)
            or not _valid_time(self.snapshot.observed_at)
            or type(self.snapshot.skipped_invalid_row_count) is not int
            or self.snapshot.skipped_invalid_row_count < 0
            or not self.snapshot.selected_clan_codes
            or len(set(self.snapshot.selected_clan_codes))
                != len(self.snapshot.selected_clan_codes)
            or any(code not in CLAN_LEADERSHIP_CHANNELS
                   for code in self.snapshot.selected_clan_codes)
            or len(rows) > 700
            or len({row.player_tag for row in rows}) != len(rows)
            or any(not _valid_row(row, self.snapshot.selected_clan_codes)
                   for row in rows)
            or rows != tuple(sorted(rows, key=lambda item: (
                self.snapshot.selected_clan_codes.index(item.clan_code),
                item.display_name.casefold(), item.player_name.casefold(),
                item.player_tag,
            )))
        ):
            raise ValueError("Invalid Missing Elder report")
        object.__setattr__(self, "retained_bytes", len(json.dumps(
            self.storage_payload(), ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")))

    @property
    def source_channels(self) -> frozenset[int]:
        return frozenset(self.source_channel_ids)

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "guild_id": self.guild_id,
            "source_channel_ids": self.source_channel_ids,
            "omitted_inaccessible_clan_codes": (
                self.omitted_inaccessible_clan_codes
            ),
            "snapshot": asdict(self.snapshot),
        }

    def manifest(self) -> dict[str, Any]:
        counts = Counter(row.clan_code for row in self.snapshot.rows)
        return {
            "report_id": self.report_id,
            "kind": "missing_elder",
            "observed_at": self.snapshot.observed_at,
            "selected_clan_codes": list(self.snapshot.selected_clan_codes),
            "omitted_inaccessible_clan_codes": list(
                self.omitted_inaccessible_clan_codes
            ),
            "missing_account_count": len(self.snapshot.rows),
            "missing_account_counts_by_clan": {
                code: counts.get(code, 0)
                for code in self.snapshot.selected_clan_codes
            },
            "skipped_invalid_row_count": (
                self.snapshot.skipped_invalid_row_count
            ),
            "complete_for_accessible_selected_clans": True,
            "calculation_scope": (
                "current linked family-clan accounts whose Discord member has "
                "the Elder role, is not Lead Plus, and is below Elder in-game"
            ),
        }

    def page(
        self, *, clan_code: str | None = None, member_id: int | None = None,
        offset: int = 0, limit: int = 25,
    ) -> dict[str, Any]:
        if (
            clan_code is not None and clan_code not in self.snapshot.selected_clan_codes
            or member_id is not None and (type(member_id) is not int or member_id <= 0)
            or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 25
        ):
            raise ValueError("Invalid Missing Elder report page")
        rows = tuple(row for row in self.snapshot.rows if (
            (clan_code is None or row.clan_code == clan_code)
            and (member_id is None or row.member_id == member_id)
        ))
        return {
            **self.manifest(),
            "filters": {"clan_code": clan_code, "member_id": member_id},
            "matching_rows": len(rows),
            "accounts": [asdict(row) for row in rows[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "complete_snapshot": True,
        }


def _valid_row(row: Any, selected: tuple[str, ...]) -> bool:
    return bool(
        type(row.member_id) is int and row.member_id > 0
        and isinstance(row.display_name, str) and row.display_name
        and len(row.display_name) <= 100
        and normalize_player_tag(row.player_tag) == row.player_tag
        and isinstance(row.player_name, str) and row.player_name
        and len(row.player_name) <= 100
        and row.clan_code in selected
        and isinstance(row.ingame_role, str) and len(row.ingame_role) <= 40
    )


def _valid_time(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


__all__ = ["MissingElderReport"]

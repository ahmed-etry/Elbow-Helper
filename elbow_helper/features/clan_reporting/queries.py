"""Typed, read-only projections of clan-reporting leadership evidence."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from elbow_helper.configuration.channels import CLAN_LEADERSHIP_CHANNELS
from elbow_helper.domain.player_tags import normalize_player_tag


MAX_MISSING_ELDER_ROWS_PER_CLAN = 100


@dataclass(frozen=True, slots=True)
class MissingElderRow:
    member_id: int
    display_name: str
    player_tag: str
    player_name: str
    clan_code: str
    ingame_role: str


@dataclass(frozen=True, slots=True)
class MissingElderSnapshot:
    observed_at: str
    selected_clan_codes: tuple[str, ...]
    skipped_invalid_row_count: int
    rows: tuple[MissingElderRow, ...]


class ClanReportingQueries:
    """Read existing clan-reporting results without exposing board controls."""

    def __init__(
        self,
        missing_elder_rows: Callable[[str], Sequence[Mapping[str, Any]]],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._missing_elder_rows = missing_elder_rows
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def missing_elder_snapshot(
        self, clan_codes: Sequence[str],
    ) -> MissingElderSnapshot:
        selected = tuple(clan_codes)
        if (
            not selected or len(selected) > len(CLAN_LEADERSHIP_CHANNELS)
            or len(set(selected)) != len(selected)
            or any(code not in CLAN_LEADERSHIP_CHANNELS for code in selected)
        ):
            raise ValueError("Invalid missing-Elder clan selection")
        rows = []
        skipped = 0
        seen_tags = set()
        for clan_code in selected:
            source_rows = self._missing_elder_rows(clan_code)
            if (
                not isinstance(source_rows, Sequence)
                or isinstance(source_rows, (str, bytes))
                or len(source_rows) > MAX_MISSING_ELDER_ROWS_PER_CLAN
            ):
                raise ValueError("Invalid missing-Elder source rows")
            for value in source_rows:
                row = _row(value, clan_code)
                if row is None or row.player_tag in seen_tags:
                    skipped += 1
                    continue
                seen_tags.add(row.player_tag)
                rows.append(row)
        rows.sort(key=lambda item: (
            selected.index(item.clan_code), item.display_name.casefold(),
            item.player_name.casefold(), item.player_tag,
        ))
        observed = self._clock()
        if not isinstance(observed, datetime):
            raise ValueError("Invalid missing-Elder observation time")
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        observed = observed.astimezone(timezone.utc)
        return MissingElderSnapshot(
            observed_at=observed.isoformat(),
            selected_clan_codes=selected,
            skipped_invalid_row_count=skipped,
            rows=tuple(rows),
        )


def _row(value: Any, clan_code: str) -> MissingElderRow | None:
    if not isinstance(value, Mapping) or value.get("clan_code") != clan_code:
        return None
    member_id = value.get("discord_user_id")
    display_name = value.get("discord_display_name")
    player_tag = normalize_player_tag(str(value.get("player_tag") or ""))
    player_name = value.get("player_name")
    ingame_role = value.get("ingame_role")
    if (
        type(member_id) is not int or member_id <= 0
        or not isinstance(display_name, str) or not display_name
        or len(display_name) > 100
        or player_tag is None
        or not isinstance(player_name, str) or not player_name
        or len(player_name) > 100
        or not isinstance(ingame_role, str) or len(ingame_role) > 40
    ):
        return None
    return MissingElderRow(
        member_id, display_name, player_tag, player_name, clan_code, ingame_role,
    )


__all__ = [
    "ClanReportingQueries", "MAX_MISSING_ELDER_ROWS_PER_CLAN",
    "MissingElderRow", "MissingElderSnapshot",
]

"""Neutral tabular-export contracts shared by feature services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExportColumn:
    """One visible spreadsheet column and its presentation metadata."""

    name: str
    width_px: int
    align: str = "left"
    note: str = ""


@dataclass(frozen=True)
class ExportSheet:
    """A structured spreadsheet tab ready for workbook or Google rendering."""

    title: str
    columns: tuple[ExportColumn, ...]
    rows: tuple[tuple[Any, ...], ...]
    tab_color: str
    dropdowns: tuple[tuple[int, tuple[str, ...]], ...] = ()

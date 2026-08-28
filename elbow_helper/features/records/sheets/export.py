"""Leadership record workbook export."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from elbow_helper.infrastructure.exports import WorkbookWriter

from ..domain.types import RECORD_CATEGORIES
from .rows import category_incident_rows
from .rows import incident_rows
from .rows import member_rows

RECORD_TAB_ACCENTS = {
    "Members": "3B5B92",
    "CWL": "D4A017",
    "War": "B52E2E",
    "Membership": "2F7D73",
    "Communication": "C47F17",
    "All Records": "64748B",
}


class RecordWorkbookWriter:
    """Render feature-owned record sheets through shared XLSX packaging."""

    def __init__(self, workbook_writer: WorkbookWriter):
        self._workbook_writer = workbook_writer

    @staticmethod
    def _apply_record_tab_accents(
        workbook_path: Path,
        sheets: list[tuple[str, list[list[Any]]]],
    ) -> None:
        worksheet_accents = {
            f"xl/worksheets/sheet{index}.xml": RECORD_TAB_ACCENTS.get(name)
            for index, (name, _) in enumerate(sheets, start=1)
        }
        namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        ElementTree.register_namespace("", namespace)
        temporary_path = workbook_path.with_name(f"{workbook_path.name}.tabs.tmp")
        try:
            with zipfile.ZipFile(workbook_path, "r") as source:
                with zipfile.ZipFile(temporary_path, "w") as target:
                    for item in source.infolist():
                        data = source.read(item.filename)
                        accent = worksheet_accents.get(item.filename)
                        if accent:
                            root = ElementTree.fromstring(data)
                            sheet_properties = root.find(f"{{{namespace}}}sheetPr")
                            if sheet_properties is None:
                                sheet_properties = ElementTree.Element(f"{{{namespace}}}sheetPr")
                                root.insert(0, sheet_properties)
                            tab_color = sheet_properties.find(f"{{{namespace}}}tabColor")
                            if tab_color is None:
                                tab_color = ElementTree.SubElement(
                                    sheet_properties,
                                    f"{{{namespace}}}tabColor",
                                )
                            tab_color.set("rgb", f"FF{accent}")
                            data = ElementTree.tostring(
                                root,
                                encoding="utf-8",
                                xml_declaration=True,
                            )
                        target.writestr(item, data)
            os.replace(temporary_path, workbook_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    @staticmethod
    def _build_record_workbook_sheets(
        records: list[dict[str, Any]],
        links_by_user: dict[int, list[dict[str, Any]]],
        *,
        include_empty_categories: bool,
    ) -> list[tuple[str, list[list[Any]]]]:
        sheets = [("Members", member_rows(records, links_by_user))]
        present_categories = {
            str(record.get("category_key") or "")
            for record in records
        }
        for category in RECORD_CATEGORIES:
            if not include_empty_categories and category.key not in present_categories:
                continue
            sheets.append((
                category.label,
                category_incident_rows(records, links_by_user, category.key),
            ))
        sheets.append(("All Records", incident_rows(records, links_by_user)))
        return sheets

    def write(
        self,
        workbook_path: Path,
        records: list[dict[str, Any]],
        links_by_user: dict[int, list[dict[str, Any]]],
        *,
        include_empty_categories: bool,
    ) -> None:
        sheets = self._build_record_workbook_sheets(
            records,
            links_by_user,
            include_empty_categories=include_empty_categories,
        )
        self._workbook_writer.write(
            workbook_path,
            sheets,
        )
        self._apply_record_tab_accents(workbook_path, sheets)

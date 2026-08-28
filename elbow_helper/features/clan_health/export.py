
"""Spreadsheet export and CWL handoff for clan health."""

from __future__ import annotations

import asyncio
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple
from xml.sax.saxutils import escape as xml_escape

import discord
import logging

from elbow_helper.infrastructure.exports import unique_sheet_name
from elbow_helper.infrastructure.exports import xlsx_column_name

from .config import EXPORT_DIR

LOGGER = logging.getLogger(__name__)


class ClanHealthExportMixin:
    def _build_health_sheet_xml(self, _sheet_name: str, rows: List[List[Any]]) -> str:
        max_cols = max((len(row) for row in rows), default=1)
        row_count = max(len(rows), 1)
        col_widths: Dict[int, int] = {}

        def is_blank(value: Any) -> bool:
            return value is None or (isinstance(value, str) and not value.strip())

        for row in rows:
            # Skip banner/explainer rows (single populated cell plus blank trailing cells) so their
            # long overflow text doesn't inflate the first column's width.
            populated_count = sum(1 for value in row if not is_blank(value))
            if len(row) >= 2 and populated_count == 1 and row and not is_blank(row[0]):
                continue
            for idx, value in enumerate(row, start=1):
                text = "" if value is None else str(value)
                col_widths[idx] = max(col_widths.get(idx, 0), len(text))
        integer_headers = {
            "members",
            "needs review",
            "watch",
            "healthy",
            "th",
            "hero sum",
            "pet sum",
            "equipment sum",
            "troop sum",
            "spell sum",
            "th delta",
            "hero delta",
            "pet delta",
            "equipment delta",
            "troop delta",
            "spell delta",
            "capital delta",
            "clan games score",
            "clan games delta",
            "cg gain",
            "warnings",
            "total players",
        }
        decimal_markers = ("actual", "expected", "delta", "adjustment", "destruction", "score", "ratio")
        wrapped_text_headers = {
            "reason",
            "why",
            "read",
            "readability",
            "flags",
            "interpretation",
            "top signals",
            "computation",
            "why it is used",
            "note",
            "context",
            "what it shows",
            "how to read it",
            "how it was judged",
            "target / expectation",
            "example",
            "primary warning",
            "next step",
            "top concerns",
            "recommended focus",
            "score interpretation",
            "notes",
            "temperature",
            "penalty / context",
        }
        tab_color_map = {
            "overview": "FF2F5597",
            "review grid": "FF0F766E",
            "guide": "FF7F8C8D",
            "timeline": "FF6C5CE7",
            "history": "FF6C5CE7",
            "clan history": "FF6C5CE7",
            "clan games": "FFF39C12",
            "signals": "FFF39C12",
            "breakdown": "FFF39C12",
            "war log": "FFC0392B",
            "war": "FFC0392B",
            "wars": "FFC0392B",
            "cwl": "FFD4AF37",
            "raid log": "FF8E44AD",
            "raids": "FF8E44AD",
            "roster fit": "FF1F618D",
            "war intel": "FFC0392B",
            "raid intel": "FF8E44AD",
            "clan games intel": "FFF39C12",
            "activity": "FF16A085",
            "context": "FF2980B9",
            "progression": "FF16A085",
            "donations": "FF2980B9",
            "trend intel": "FF6C5CE7",
            "signals used": "FFF39C12",
            "metadata": "FF7F8C8D",
            "summary": "FF2F5597",
            "metrics": "FF16A085",
            "signal coverage": "FFF39C12",
            "trend history": "FF6C5CE7",
            "clan movement": "FF0984E3",
        }
        section_headers: Dict[int, Dict[int, str]] = {}
        for idx, row in enumerate(rows, start=1):
            if not row:
                continue
            first = str(row[0]).strip().lower() if row[0] is not None else ""
            row_values = [str(v).strip().lower() for v in row if v is not None and str(v).strip()]
            is_clan_block = first == "clan" and ("health" in row_values or "player" in row_values)
            is_verdict_block = first == "verdict" and "player" in row_values
            if is_clan_block or is_verdict_block:
                # Supports repeated header blocks (e.g., Overview + Leadership Queue, or explainer-prefixed tables).
                section_headers[idx] = {
                    col_idx: str(value).strip().lower()
                    for col_idx, value in enumerate(row, start=1)
                    if value is not None
                }

        def resolve_header(row_idx: int, col_idx: int) -> str:
            if section_headers:
                active_idx = 0
                for hdr_idx in section_headers:
                    if hdr_idx <= row_idx and hdr_idx >= active_idx:
                        active_idx = hdr_idx
                if active_idx:
                    return section_headers[active_idx].get(col_idx, "")
            return header_lookup.get(col_idx, "")

        sheet_key_lower = str(_sheet_name or "").strip().lower()
        first_section_header_idx = min(section_headers) if section_headers else 0
        primary_header_idx = first_section_header_idx or 1
        header_row = rows[primary_header_idx - 1] if rows and primary_header_idx <= len(rows) else []
        header_lookup = {
            idx: str(value).strip().lower()
            for idx, value in enumerate(header_row, start=1)
            if not is_blank(value)
        }
        merged_banner_rows = [
            row_idx
            for row_idx, row in enumerate(rows, start=1)
            if row_idx < first_section_header_idx
            and len(row) >= 2
            and not is_blank(row[0])
            and all(is_blank(value) for value in row[1:])
        ]

        def cell_style(row_idx: int, col_idx: int, value: Any) -> int:
            if row_idx == 1:
                return 1  # Header
            if sheet_key_lower == "overview":
                row_values = [str(v).strip().lower() for v in rows[row_idx - 1] if v is not None]
                if row_values:
                    if row_values[0] == "clan" and ("health" in row_values or "player" in row_values):
                        return 1
                    if row_values[0] == "verdict" and "player" in row_values:
                        return 1
                # Explainer rows sit between the title banner and the first section header.
                if first_section_header_idx and 1 < row_idx < first_section_header_idx:
                    return 11
            header = resolve_header(row_idx, col_idx)
            if isinstance(value, str):
                value_text = value.strip().lower()
                if header in {"status", "health", "verdict", "confidence", "result"}:
                    if value_text == "needs review":
                        return 8
                    if value_text in {"watch", "monitor next window"}:
                        return 9
                    if value_text in {"healthy", "good", "no action needed"}:
                        return 10
                    if value_text in {"not tracked", "insufficient data"}:
                        return 9
                    if value_text == "needs review":
                        return 8
                    if value_text in {"good fit", "strong", "good", "high"}:
                        return 10
                    if value_text == "expected":
                        return 9
                    if value_text == "below":
                        return 8
                    if value_text == "empty":
                        return 8
                    if value_text == "partial":
                        return 9
                    if value_text == "available":
                        return 10
                    if value_text in {"no records", "insufficient sample"}:
                        return 8
                    if value_text == "not started":
                        return 9
                    if value_text == "medium":
                        return 9
                    if value_text == "low":
                        return 8
                    if header == "result":
                        if value_text in {"ok", "healthy", "good"}:
                            return 10
                        if value_text == "watch":
                            return 9
                        if value_text in {"did not participate", "no activity", "needs review"}:
                            return 8
                        if value_text in {"not tracked", "not enough data", "insufficient data"}:
                            return 9
                if header in {
                    "participation",
                    "performance",
                    "progression",
                    "support",
                    "donations",
                    "war hit rate",
                    "raid hit rate",
                    "clan games active rate",
                    "participation score",
                    "performance score",
                    "progress score",
                    "support score",
                    "donation score",
                }:
                    if value_text.startswith("strong"):
                        return 10
                    if value_text.startswith("mixed"):
                        return 9
                    if value_text.startswith("weak"):
                        return 8
                    if value_text == "no data":
                        return 9
                if header in {"support band", "donation band", "donations"}:
                    if value_text in {"strong", "target met", "excellent"}:
                        return 10
                    if value_text in {"moderate", "near target", "low volume", "no baseline", "insufficient baseline"}:
                        return 9
                    if value_text in {"low", "below target", "low contribution", "no activity (0/0)"}:
                        return 8
                if header == "health":
                    if value_text == "at risk":
                        return 8
                    if value_text == "monitor":
                        return 9
                    if value_text == "healthy":
                        return 10
                if header == "fit":
                    if value_text == "poor fit":
                        return 8
                    if value_text == "borderline":
                        return 9
                    if value_text == "good fit":
                        return 10
                if header == "priority":
                    if value_text == "high":
                        return 8
                    if value_text == "medium":
                        return 9
                    if value_text == "low":
                        return 10
                if header == "clan games score band":
                    if value_text in {"maxed event", "strong"}:
                        return 10
                    if value_text in {"low", "no baseline"}:
                        return 9
                    if value_text == "no contribution":
                        return 8
                if header.startswith("trend"):
                    if value_text.startswith("getting better"):
                        return 10
                    if value_text.startswith("getting worse"):
                        return 8
                    if value_text.startswith("no change") or value_text == "not enough history to compare":
                        return 9
                if header in {"warning", "primary warning"}:
                    if value_text in {"ok", "no major issues"}:
                        return 10
                    if "no " in value_text and "data" in value_text:
                        return 9
                    if value_text in {"low request volume", "low volume", "no activity data", "raid participation baseline is estimated"}:
                        return 9
                    return 8
            zebra = (row_idx % 2) == 0
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                is_integer = header in integer_headers
                if not is_integer and isinstance(value, int):
                    is_integer = True
                if not is_integer and isinstance(value, float):
                    is_decimal_column = any(marker in header for marker in decimal_markers)
                    is_integer = float(value).is_integer() and not is_decimal_column
                if is_integer:
                    return 7 if zebra else 4
                return 6 if zebra else 3
            if header in wrapped_text_headers:
                return 12 if zebra else 11
            return 5 if zebra else 2

        sheet_key = str(_sheet_name or "").strip().lower()
        tab_color = tab_color_map.get(sheet_key)
        if tab_color is None:
            if sheet_key.endswith(" health"):
                tab_color = "FF1F618D"
            elif sheet_key.endswith(" intel"):
                tab_color = "FF16A085"
        tab_xml = f'<sheetPr><tabColor rgb="{tab_color}"/></sheetPr>' if tab_color else ""
        if sheet_key_lower == "overview":
            sheet_views_xml = '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
        else:
            sheet_views_xml = (
                f'<sheetViews><sheetView workbookViewId="0"><pane ySplit="{first_section_header_idx or 1}" '
                f'topLeftCell="A{(first_section_header_idx or 1) + 1}" activePane="bottomLeft" '
                'state="frozen"/></sheetView></sheetViews>'
            )
        parts: List[str] = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
            tab_xml,
            f'<dimension ref="A1:{xlsx_column_name(max_cols)}{row_count}"/>',
            sheet_views_xml,
            "<cols>",
        ]
        for col_idx in range(1, max_cols + 1):
            header = header_lookup.get(col_idx, "")
            min_width = 10
            if header == "verdict":
                min_width = 14
            elif header in {"player", "name"}:
                min_width = 22
            elif header == "why":
                min_width = 48
            width = min(62, max(min_width, col_widths.get(col_idx, 10) + 2))
            parts.append(f'<col min="{col_idx}" max="{col_idx}" width="{width}" customWidth="1"/>')
        parts.append("</cols><sheetData>")

        for row_idx, row in enumerate(rows, start=1):
            parts.append(f'<row r="{row_idx}">')
            for col_idx, value in enumerate(row, start=1):
                if value is None:
                    continue
                cell_ref = f"{xlsx_column_name(col_idx)}{row_idx}"
                style_attr = f' s="{cell_style(row_idx, col_idx, value)}"'
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    parts.append(f'<c r="{cell_ref}"{style_attr}><v>{value}</v></c>')
                else:
                    text = xml_escape(str(value)).replace("\n", "&#10;")
                    parts.append(
                        f'<c r="{cell_ref}" t="inlineStr"{style_attr}><is><t xml:space="preserve">{text}</t></is></c>'
                    )
            parts.append("</row>")

        parts.append("</sheetData>")
        filter_row = first_section_header_idx or 1
        if row_count >= filter_row:
            parts.append(
                f'<autoFilter ref="A{filter_row}:'
                f'{xlsx_column_name(max_cols)}{filter_row}"/>'
            )
        if merged_banner_rows:
            parts.append(f'<mergeCells count="{len(merged_banner_rows)}">')
            last_col = xlsx_column_name(max_cols)
            for row_idx in merged_banner_rows:
                parts.append(f'<mergeCell ref="A{row_idx}:{last_col}{row_idx}"/>')
            parts.append("</mergeCells>")

        if row_count >= 2:
            cf_priority = 1
            score_heatmap_headers = {
                "overall score",
                "war score",
                "raid score",
                "clan games score",
                "progress score",
                "support score",
                "donation score",
                "final delta",
            }
            for col_idx, header in header_lookup.items():
                if header in score_heatmap_headers:
                    # Score columns get red->yellow->green gradient.
                    col_name = xlsx_column_name(col_idx)
                    sqref = f"{col_name}2:{col_name}{row_count}"
                    parts.append(f'<conditionalFormatting sqref="{sqref}">')
                    parts.append(
                        f'<cfRule type="colorScale" priority="{cf_priority}"><colorScale>'
                        '<cfvo type="min"/><cfvo type="num" val="50"/><cfvo type="max"/>'
                        '<color rgb="FFF8696B"/><color rgb="FFFFEB84"/><color rgb="FF63BE7B"/>'
                        "</colorScale></cfRule>"
                    )
                    parts.append("</conditionalFormatting>")
                    cf_priority += 1
                    continue
                if header == "stars":
                    col_name = xlsx_column_name(col_idx)
                    sqref = f"{col_name}2:{col_name}{row_count}"
                    parts.append(f'<conditionalFormatting sqref="{sqref}">')
                    parts.append(
                        f'<cfRule type="colorScale" priority="{cf_priority}"><colorScale>'
                        '<cfvo type="num" val="0"/><cfvo type="num" val="2"/><cfvo type="num" val="3"/>'
                        '<color rgb="FFF8696B"/><color rgb="FFFFEB84"/><color rgb="FF63BE7B"/>'
                        "</colorScale></cfRule>"
                    )
                    parts.append("</conditionalFormatting>")
                    cf_priority += 1
                    continue
                if header in {"destruction", "destruction %"}:
                    col_name = xlsx_column_name(col_idx)
                    sqref = f"{col_name}2:{col_name}{row_count}"
                    parts.append(f'<conditionalFormatting sqref="{sqref}">')
                    parts.append(
                        f'<cfRule type="colorScale" priority="{cf_priority}"><colorScale>'
                        '<cfvo type="num" val="0"/><cfvo type="num" val="70"/><cfvo type="num" val="100"/>'
                        '<color rgb="FFF8696B"/><color rgb="FFFFEB84"/><color rgb="FF63BE7B"/>'
                        "</colorScale></cfRule>"
                    )
                    parts.append("</conditionalFormatting>")
                    cf_priority += 1
                    continue
                if header == "needs %":
                    # Needs% is inverted: lower is better (green).
                    col_name = xlsx_column_name(col_idx)
                    sqref = f"{col_name}2:{col_name}{row_count}"
                    parts.append(f'<conditionalFormatting sqref="{sqref}">')
                    parts.append(
                        f'<cfRule type="colorScale" priority="{cf_priority}"><colorScale>'
                        '<cfvo type="min"/><cfvo type="num" val="15"/><cfvo type="max"/>'
                        '<color rgb="FF63BE7B"/><color rgb="FFFFEB84"/><color rgb="FFF8696B"/>'
                        "</colorScale></cfRule>"
                    )
                    parts.append("</conditionalFormatting>")
                    cf_priority += 1
                    continue
                if not ("delta" in header or "adjustment" in header):
                    continue
                # Delta-like columns use zero-centered gradient.
                col_name = xlsx_column_name(col_idx)
                sqref = f"{col_name}2:{col_name}{row_count}"
                parts.append(f'<conditionalFormatting sqref="{sqref}">')
                parts.append(
                    f'<cfRule type="colorScale" priority="{cf_priority}"><colorScale>'
                    '<cfvo type="min"/><cfvo type="num" val="0"/><cfvo type="max"/>'
                    '<color rgb="FFF8696B"/><color rgb="FFFFEB84"/><color rgb="FF63BE7B"/>'
                    "</colorScale></cfRule>"
                )
                parts.append("</conditionalFormatting>")
                cf_priority += 1

        parts.append("</worksheet>")
        return "".join(parts)

    def _write_health_xlsx_file(self, file_path: Path, sheets: List[Tuple[str, List[List[Any]]]]) -> None:
        used_names: Set[str] = set()
        normalized = [
            (unique_sheet_name(name, used_names), rows)
            for name, rows in sheets
        ]
        # Build XLSX parts directly to avoid runtime dependency on spreadsheet libs.

        workbook_xml_parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ',
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
            "<sheets>",
        ]
        for idx, (name, _) in enumerate(normalized, start=1):
            workbook_xml_parts.append(
                f'<sheet name="{xml_escape(name)}" sheetId="{idx}" r:id="rId{idx}"/>'
            )
        workbook_xml_parts.append("</sheets></workbook>")
        workbook_xml = "".join(workbook_xml_parts)

        workbook_rels_parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
        ]
        for idx, _ in enumerate(normalized, start=1):
            workbook_rels_parts.append(
                f'<Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>'
            )
        workbook_rels_parts.append(
            f'<Relationship Id="rId{len(normalized) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        )
        workbook_rels_parts.append("</Relationships>")
        workbook_rels_xml = "".join(workbook_rels_parts)

        content_types_parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
            '<Default Extension="xml" ContentType="application/xml"/>',
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
        ]
        for idx, _ in enumerate(normalized, start=1):
            content_types_parts.append(
                f'<Override PartName="/xl/worksheets/sheet{idx}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            )
        content_types_parts.append("</Types>")
        content_types_xml = "".join(content_types_parts)

        styles_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="3">'
            '<font><sz val="11"/><name val="Calibri"/></font>'
            '<font><b/><sz val="11"/><name val="Calibri"/></font>'
            '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font>'
            '</fonts>'
            '<fills count="7">'
            '<fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FF0F766E"/><bgColor indexed="64"/></patternFill></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFF5F7FB"/><bgColor indexed="64"/></patternFill></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFFCE8E6"/><bgColor indexed="64"/></patternFill></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFFFF4E5"/><bgColor indexed="64"/></patternFill></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFEAF6EE"/><bgColor indexed="64"/></patternFill></fill>'
            '</fills>'
            '<borders count="2">'
            '<border><left/><right/><top/><bottom/><diagonal/></border>'
            '<border><left style="thin"><color auto="1"/></left><right style="thin"><color auto="1"/></right><top style="thin"><color auto="1"/></top><bottom style="thin"><color auto="1"/></bottom><diagonal/></border>'
            '</borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="13">'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>'
            '<xf numFmtId="2" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>'
            '<xf numFmtId="1" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>'
            '<xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1"/>'
            '<xf numFmtId="2" fontId="0" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFill="1" applyBorder="1"/>'
            '<xf numFmtId="1" fontId="0" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFill="1" applyBorder="1"/>'
            '<xf numFmtId="0" fontId="1" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
            '<xf numFmtId="0" fontId="1" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
            '<xf numFmtId="0" fontId="1" fillId="6" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
            '<xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
            '</cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            '</styleSheet>'
        )

        root_rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        )

        file_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(file_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", content_types_xml)
            zf.writestr("_rels/.rels", root_rels_xml)
            zf.writestr("xl/workbook.xml", workbook_xml)
            zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
            zf.writestr("xl/styles.xml", styles_xml)
            for idx, (name, rows) in enumerate(normalized, start=1):
                zf.writestr(f"xl/worksheets/sheet{idx}.xml", self._build_health_sheet_xml(name, rows))

    async def _write_and_send_export(
        self,
        *,
        interaction: discord.Interaction,
        workbook_name: str,
        workbook_title: str,
        summary_lines: List[str],
        sheets: List[Tuple[str, List[List[Any]]]],
    ) -> None:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        workbook_path = EXPORT_DIR / workbook_name
        try:
            await asyncio.to_thread(self._write_health_xlsx_file, workbook_path, sheets)
        except (OSError, TypeError, ValueError) as e:
            LOGGER.exception("Export write failed for %s: %s", workbook_path, e)
            await interaction.followup.send("Could not generate the spreadsheet right now. Try again in a moment.")
            return

        google_link = None
        google_warning = None
        google_link, google_warning = await self.google_publisher.upload_workbook(
            workbook_path,
            workbook_title,
            cleanup_name_contains="Health",
            retention_days=0,
        )

        export_view = discord.ui.View(timeout=None)
        google_download_link = None
        if google_link:
            sheet_match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", google_link)
            if sheet_match:
                google_download_link = (
                    f"https://docs.google.com/spreadsheets/d/{sheet_match.group(1)}/export?format=xlsx"
                )
            export_view.add_item(
                discord.ui.Button(label="Google Sheet", style=discord.ButtonStyle.link, url=google_link)
            )
        if google_download_link:
            export_view.add_item(
                discord.ui.Button(label="Download", style=discord.ButtonStyle.link, url=google_download_link)
            )
        if google_link and export_view.children:
            await interaction.followup.send(content=workbook_title, view=export_view)
            return

        if google_warning:
            summary_lines.append(google_warning)
            LOGGER.warning("Google warning: %s", google_warning)
        msg = await interaction.followup.send(
            "\n".join(summary_lines),
            wait=True,
            file=discord.File(str(workbook_path), filename=workbook_name),
        )
        if msg.attachments:
            export_view.add_item(
                discord.ui.Button(label="Download", style=discord.ButtonStyle.link, url=msg.attachments[0].url)
            )
        if export_view.children:
            await msg.edit(content=workbook_title, view=export_view)

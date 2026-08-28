"""CWL bonus workbook generation."""

from __future__ import annotations

import logging
import math
import re
import zipfile
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple
from xml.sax.saxutils import escape as xml_escape

from elbow_helper.infrastructure.exports import unique_sheet_name
from elbow_helper.infrastructure.exports import xlsx_column_name

LOGGER = logging.getLogger(__name__)

BONUS_WORKBOOK_CLAN_ACCENTS = {
    "BEH": "B52E2E",
    "BE4": "AC5414",
    "BES": "604D48",
    "BE1": "975C4F",
    "BEM": "3E83AE",
    "BEC": "E09F55",
    "BEP": "4B6950",
    "BEE": "988007",
}
BONUS_WORKBOOK_NEUTRAL_ACCENT = "64748B"
BONUS_WORKBOOK_HEADER_FILL = "374151"


class BonusWorkbookWriter:
    """Render the feature-specific CWL bonus workbook."""

    def guide_sheet(self) -> List[List[Any]]:
        return [
            ["Metric", "Explanation", "Example"],
            [
                "Actual Score (AS)",
                "The attack result expressed as a score. A 3-star attack is worth 3.00; lower results combine stars and destruction. If the base was already attacked, only additional improvement is credited.",
                "A 2-star attack with 80% destruction scores 2.80.",
            ],
            [
                "Expected Score (ES)",
                "The result expected for the attacker and defender Town Hall matchup under the clan's scoring setup.",
                "A TH17 attacking a TH18 is compared with the clan's TH17-vs-TH18 expected score.",
            ],
            [
                "Base Delta",
                "How far the attack performed above or below its expected result before any uphit or downhit adjustment.",
                "Actual score 2.80 minus expected score 2.60 gives +0.20.",
            ],
        ]


    @staticmethod
    def _bonus_sheet_clan_code(sheet_name: str) -> Optional[str]:
        match = re.match(r"^(BEH|BE4|BES|BE1|BEM|BEC|BEP|BEE)(?:_|$)", sheet_name)
        return match.group(1) if match else None


    def _bonus_sheet_accent(self, sheet_name: str) -> str:
        clan_code = self._bonus_sheet_clan_code(sheet_name)
        return BONUS_WORKBOOK_CLAN_ACCENTS.get(clan_code or "", BONUS_WORKBOOK_NEUTRAL_ACCENT)


    @staticmethod
    def _bonus_is_raw_sheet(sheet_name: str) -> bool:
        normalized = sheet_name.replace(" ", "_")
        return normalized == "Raw_Attacks" or normalized.endswith("_Raw")


    @staticmethod
    def _bonus_column_width(header: str, measured: int) -> int:
        normalized = str(header or "").strip().lower()
        if normalized == "explanation":
            return 55
        if normalized == "example":
            return 42
        if normalized == "metric":
            return 20
        if normalized in {"player", "ineligible players"}:
            return 22
        if normalized in {"clan", "flags (if any)", "reason"}:
            return 18
        if normalized in {"defender tag"}:
            return 16
        if normalized in {"rank", "round", "stars", "star gain", "attacks"}:
            return 10
        if normalized in {"attacker th", "defender th", "th gap (def-att)"}:
            return 13
        if "avg " in normalized or "score" in normalized or "delta" in normalized or "adjustment" in normalized:
            return 18
        if "destruction" in normalized:
            return 15
        return min(28, max(10, measured + 2))


    @staticmethod
    def _bonus_guide_row_height(row: List[Any]) -> int:
        column_widths = (20, 55, 42)
        wrapped_lines = 1
        for index, value in enumerate(row):
            width = column_widths[index] if index < len(column_widths) else 20
            text = str(value or "")
            explicit_lines = text.splitlines() or [""]
            cell_lines = sum(max(1, math.ceil(len(line) / max(1, width - 2))) for line in explicit_lines)
            wrapped_lines = max(wrapped_lines, cell_lines)
        return min(120, max(30, 8 + (wrapped_lines * 15)))


    def _build_sheet_xml(
        self,
        sheet_name: str,
        rows: List[List[Any]],
        *,
        sheet_accent: str,
        header_style_id: int,
    ) -> str:
        max_cols = max((len(row) for row in rows), default=1)
        row_count = max(len(rows), 1)
        col_widths: Dict[int, int] = {}
        for row in rows:
            for idx, value in enumerate(row, start=1):
                text = "" if value is None else str(value)
                col_widths[idx] = max(col_widths.get(idx, 0), len(text))
        header_row = rows[0] if rows else []
        header_lookup = {
            idx: str(value).strip().lower()
            for idx, value in enumerate(header_row, start=1)
            if value is not None
        }
        integer_headers = {
            "rank",
            "attacks",
            "flagged attacks",
            "missed attacks",
            "round",
            "attacker th",
            "defender th",
            "stars",
            "expected attacks",
            "used attacks",
            "warnings count",
        }
        decimal_markers = (
            "actual",
            "expected",
            "delta",
            "adjustment",
            "destruction",
        )
        is_raw_sheet = self._bonus_is_raw_sheet(sheet_name)
        raw_round_groups: Dict[int, int] = {}
        raw_round_boundaries: Set[int] = set()
        if is_raw_sheet and header_lookup.get(1) == "round":
            previous_round: Any = object()
            group_index = -1
            for row_idx, row in enumerate(rows[1:], start=2):
                round_value = row[0] if row else None
                if round_value != previous_round:
                    group_index += 1
                    if row_idx > 2:
                        raw_round_boundaries.add(row_idx)
                    previous_round = round_value
                raw_round_groups[row_idx] = group_index

        def cell_style(row_idx: int, col_idx: int, value: Any) -> int:
            # Style IDs are defined in styles.xml within write().
            if row_idx == 1:
                return header_style_id
            shaded = (
                raw_round_groups.get(row_idx, 0) % 2 == 1
                if is_raw_sheet
                else (row_idx % 2) == 0
            )
            round_boundary = is_raw_sheet and row_idx in raw_round_boundaries
            if sheet_name == "Guide":
                return 9 if shaded else 8
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                header = header_lookup.get(col_idx, "")
                is_integer = header in integer_headers
                if not is_integer and isinstance(value, int):
                    is_integer = True
                if not is_integer and isinstance(value, float):
                    is_decimal_column = any(marker in header for marker in decimal_markers)
                    is_integer = float(value).is_integer() and not is_decimal_column
                if is_integer:
                    if round_boundary:
                        return 15 if shaded else 12
                    return 7 if shaded else 4
                if round_boundary:
                    return 14 if shaded else 11
                return 6 if shaded else 3
            if round_boundary:
                return 13 if shaded else 10
            return 5 if shaded else 2

        parts: List[str] = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
            f'<sheetPr><tabColor rgb="FF{sheet_accent}"/></sheetPr>',
            f'<dimension ref="A1:{xlsx_column_name(max_cols)}{row_count}"/>',
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
            "<cols>",
        ]
        for col_idx in range(1, max_cols + 1):
            width_header = header_lookup.get(col_idx, "")
            if any(
                len(row) >= col_idx
                and str(row[col_idx - 1]).strip().lower() == "ineligible players"
                for row in rows
            ):
                width_header = "ineligible players"
            width = self._bonus_column_width(
                width_header,
                col_widths.get(col_idx, 10),
            )
            parts.append(
                f'<col min="{col_idx}" max="{col_idx}" width="{width}" customWidth="1"/>'
            )
        parts.append("</cols><sheetData>")

        for row_idx, row in enumerate(rows, start=1):
            row_height = ""
            if row_idx == 1:
                row_height = ' ht="30" customHeight="1"'
            elif sheet_name == "Guide":
                height = self._bonus_guide_row_height(row)
                row_height = f' ht="{height}" customHeight="1"'
            else:
                explicit_lines = max(
                    (str(value).count("\n") + 1 for value in row if value is not None),
                    default=1,
                )
                if explicit_lines > 1:
                    height = min(120, max(30, 8 + (explicit_lines * 15)))
                    row_height = f' ht="{height}" customHeight="1"'
            parts.append(f'<row r="{row_idx}"{row_height}>')
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
        if row_count >= 1:
            parts.append(
                f'<autoFilter ref="A1:{xlsx_column_name(max_cols)}1"/>'
            )

        # The tie-breaker metric is the only score column that receives emphasis.
        if row_count >= 2:
            for col_idx, header in header_lookup.items():
                if header != "avg final delta/attack":
                    continue
                col_name = xlsx_column_name(col_idx)
                sqref = f"{col_name}2:{col_name}{row_count}"
                parts.append(f'<conditionalFormatting sqref="{sqref}">')
                parts.append(
                    '<cfRule type="colorScale" priority="1"><colorScale>'
                    '<cfvo type="min"/><cfvo type="num" val="0"/><cfvo type="max"/>'
                    '<color rgb="FFF4CCCC"/><color rgb="FFFFFFFF"/><color rgb="FFD9EAD3"/>'
                    "</colorScale></cfRule>"
                )
                parts.append("</conditionalFormatting>")

        parts.append("</worksheet>")
        return "".join(parts)


    def write(
        self,
        file_path: Path,
        sheets: List[Tuple[str, List[List[Any]]]],
        *,
        workbook_clan_code: Optional[str] = None,
    ) -> None:
        used_names: Set[str] = set()
        normalized = [
            (unique_sheet_name(name, used_names), rows)
            for name, rows in sheets
        ]
        workbook_clans = {
            clan_code
            for name, _ in normalized
            if (clan_code := self._bonus_sheet_clan_code(name))
        }

        def workbook_accent(sheet_name: str) -> str:
            if workbook_clan_code and sheet_name in {"Summary", "Raw Attacks"}:
                return BONUS_WORKBOOK_CLAN_ACCENTS.get(
                    workbook_clan_code,
                    BONUS_WORKBOOK_NEUTRAL_ACCENT,
                )
            if self._bonus_is_raw_sheet(sheet_name) and len(workbook_clans) == 1:
                return BONUS_WORKBOOK_CLAN_ACCENTS[next(iter(workbook_clans))]
            return self._bonus_sheet_accent(sheet_name)

        accent_order: List[str] = []
        for name, _ in normalized:
            accent = workbook_accent(name)
            if accent not in accent_order:
                accent_order.append(accent)
        header_style_ids = {
            accent: 16 + index
            for index, accent in enumerate(accent_order)
        }

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

        accent_header_xfs = "".join(
            (
                '<xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" '
                'applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
                '<alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
            )
            for _ in accent_order
        )
        styles_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="3">'
            '<font><sz val="11"/><name val="Calibri"/></font>'
            '<font><b/><sz val="11"/><name val="Calibri"/></font>'
            '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font>'
            '</fonts>'
            '<fills count="4">'
            '<fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill>'
            f'<fill><patternFill patternType="solid"><fgColor rgb="FF{BONUS_WORKBOOK_HEADER_FILL}"/><bgColor indexed="64"/></patternFill></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFE6E6E6"/><bgColor indexed="64"/></patternFill></fill>'
            '</fills>'
            '<borders count="3">'
            '<border><left/><right/><top/><bottom/><diagonal/></border>'
            '<border><left/><right/><top/><bottom style="hair"><color rgb="FFE5E7EB"/></bottom><diagonal/></border>'
            '<border><left/><right/><top style="thin"><color rgb="FF94A3B8"/></top><bottom style="hair"><color rgb="FFE5E7EB"/></bottom><diagonal/></border>'
            '</borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            f'<cellXfs count="{16 + len(accent_order)}">'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
            '<xf numFmtId="2" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>'
            '<xf numFmtId="1" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>'
            '<xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
            '<xf numFmtId="2" fontId="0" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFill="1" applyBorder="1"/>'
            '<xf numFmtId="1" fontId="0" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFill="1" applyBorder="1"/>'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
            '<xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="2" xfId="0" applyBorder="1"/>'
            '<xf numFmtId="2" fontId="0" fillId="0" borderId="2" xfId="0" applyNumberFormat="1" applyBorder="1"/>'
            '<xf numFmtId="1" fontId="0" fillId="0" borderId="2" xfId="0" applyNumberFormat="1" applyBorder="1"/>'
            '<xf numFmtId="0" fontId="0" fillId="3" borderId="2" xfId="0" applyFill="1" applyBorder="1"/>'
            '<xf numFmtId="2" fontId="0" fillId="3" borderId="2" xfId="0" applyNumberFormat="1" applyFill="1" applyBorder="1"/>'
            '<xf numFmtId="1" fontId="0" fillId="3" borderId="2" xfId="0" applyNumberFormat="1" applyFill="1" applyBorder="1"/>'
            f'{accent_header_xfs}'
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
                zf.writestr(
                    f"xl/worksheets/sheet{idx}.xml",
                    self._build_sheet_xml(
                        name,
                        rows,
                        sheet_accent=workbook_accent(name),
                        header_style_id=header_style_ids[workbook_accent(name)],
                    ),
                )

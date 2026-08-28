"""Dependency-free XLSX packaging for ordinary tabular exports."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any
from typing import Sequence
import zipfile
from xml.sax.saxutils import escape as xml_escape


NEUTRAL_ACCENT = "64748B"
HEADER_FILL = "374151"


def xlsx_column_name(index: int) -> str:
    name = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def unique_sheet_name(raw_name: str, used_names: set[str]) -> str:
    sanitized = re.sub(r"[\[\]\*:/\\?]", "_", raw_name).strip() or "Sheet"
    sanitized = sanitized[:31]
    base = sanitized
    counter = 2
    while sanitized in used_names:
        suffix = f"_{counter}"
        sanitized = f"{base[:31 - len(suffix)]}{suffix}"
        counter += 1
    used_names.add(sanitized)
    return sanitized


def _sheet_xml(
    rows: Sequence[Sequence[Any]],
    *,
    tab_color: str,
) -> str:
    max_columns = max((len(row) for row in rows), default=1)
    row_count = max(len(rows), 1)
    widths: dict[int, int] = {}
    for row in rows:
        for column_index, value in enumerate(row, start=1):
            widths[column_index] = max(
                widths.get(column_index, 0),
                len("" if value is None else str(value)),
            )
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        f'<sheetPr><tabColor rgb="FF{tab_color}"/></sheetPr>',
        f'<dimension ref="A1:{xlsx_column_name(max_columns)}{row_count}"/>',
        (
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" '
            'topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            "</sheetView></sheetViews>"
        ),
        "<cols>",
    ]
    for column_index in range(1, max_columns + 1):
        width = min(60, max(10, widths.get(column_index, 10) + 2))
        parts.append(
            f'<col min="{column_index}" max="{column_index}" '
            f'width="{width}" customWidth="1"/>'
        )
    parts.append("</cols><sheetData>")
    for row_index, row in enumerate(rows, start=1):
        height = ' ht="30" customHeight="1"' if row_index == 1 else ""
        parts.append(f'<row r="{row_index}"{height}>')
        for column_index, value in enumerate(row, start=1):
            if value is None:
                continue
            cell_reference = f"{xlsx_column_name(column_index)}{row_index}"
            if row_index == 1:
                style_id = 1
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                if isinstance(value, float):
                    style_id = 3 if row_index % 2 else 7
                else:
                    style_id = 4 if row_index % 2 else 6
            else:
                style_id = 2 if row_index % 2 else 5
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                parts.append(
                    f'<c r="{cell_reference}" s="{style_id}"><v>{value}</v></c>'
                )
            else:
                text = xml_escape(str(value)).replace("\n", "&#10;")
                parts.append(
                    f'<c r="{cell_reference}" t="inlineStr" s="{style_id}">'
                    f'<is><t xml:space="preserve">{text}</t></is></c>'
                )
        parts.append("</row>")
    parts.append("</sheetData>")
    parts.append(
        f'<autoFilter ref="A1:{xlsx_column_name(max_columns)}1"/>'
    )
    parts.append("</worksheet>")
    return "".join(parts)


class WorkbookWriter:
    """Write a small, styled XLSX workbook without a heavy runtime dependency."""

    def write(
        self,
        file_path: Path,
        sheets: Sequence[tuple[str, Sequence[Sequence[Any]]]],
    ) -> None:
        used_names: set[str] = set()
        normalized = [
            (unique_sheet_name(name, used_names), rows)
            for name, rows in sheets
        ]
        workbook_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheets>"
            + "".join(
                f'<sheet name="{xml_escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
                for index, (name, _) in enumerate(normalized, start=1)
            )
            + "</sheets></workbook>"
        )
        workbook_relationships = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                (
                    f'<Relationship Id="rId{index}" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                    f'Target="worksheets/sheet{index}.xml"/>'
                )
                for index, _ in enumerate(normalized, start=1)
            )
            + (
                f'<Relationship Id="rId{len(normalized) + 1}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                'Target="styles.xml"/></Relationships>'
            )
        )
        content_types = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            + "".join(
                (
                    f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                )
                for index, _ in enumerate(normalized, start=1)
            )
            + "</Types>"
        )
        styles = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
            '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="4"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill>'
            f'<fill><patternFill patternType="solid"><fgColor rgb="FF{HEADER_FILL}"/>'
            '<bgColor indexed="64"/></patternFill></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFE6E6E6"/>'
            '<bgColor indexed="64"/></patternFill></fill></fills>'
            '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
            '<border><left/><right/><top/><bottom style="hair">'
            '<color rgb="FFE5E7EB"/></bottom><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="8">'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
            '<xf numFmtId="2" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>'
            '<xf numFmtId="1" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>'
            '<xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
            '<xf numFmtId="1" fontId="0" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFill="1" applyBorder="1"/>'
            '<xf numFmtId="2" fontId="0" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFill="1" applyBorder="1"/>'
            '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            "</styleSheet>"
        )
        root_relationships = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        )
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(
            file_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as workbook:
            workbook.writestr("[Content_Types].xml", content_types)
            workbook.writestr("_rels/.rels", root_relationships)
            workbook.writestr("xl/workbook.xml", workbook_xml)
            workbook.writestr(
                "xl/_rels/workbook.xml.rels",
                workbook_relationships,
            )
            workbook.writestr("xl/styles.xml", styles)
            for index, (_, rows) in enumerate(normalized, start=1):
                workbook.writestr(
                    f"xl/worksheets/sheet{index}.xml",
                    _sheet_xml(rows, tab_color=NEUTRAL_ACCENT),
                )

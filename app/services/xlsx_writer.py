"""A small .xlsx writer on the standard library (zipfile + XML).

Enough for exports: several sheets, a bold frozen header row, column widths,
numbers as numbers, and clickable links. Text is written as inline strings, so
a cell starting with "=" is shown as text and never evaluated as a formula.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

Cell = str | int | float | None

# Excel's limits.
_MAX_SHEET_NAME = 31
_MAX_CELL_CHARS = 32767
_MIN_WIDTH, _MAX_WIDTH = 8, 60
# XML 1.0 forbids most control characters; a stray one makes Excel refuse the file.
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass
class Sheet:
    name: str
    headers: Sequence[str]
    rows: Sequence[Sequence[Cell]]
    # Columns (0-based) whose values are URLs to make clickable.
    link_columns: frozenset[int] = field(default_factory=frozenset)


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _clean(text: str) -> str:
    return escape(_ILLEGAL_XML.sub("", text)[:_MAX_CELL_CHARS])


def _cell(ref: str, value: Cell, style: int) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        value = "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{_clean(str(value))}</t></is></c>'


def _widths(sheet: Sheet) -> str:
    cols = []
    for i, header in enumerate(sheet.headers):
        longest = max([len(str(header))] + [len(str(r[i])) for r in sheet.rows if i < len(r) and r[i] is not None])
        width = min(max(longest + 2, _MIN_WIDTH), _MAX_WIDTH)
        cols.append(f'<col min="{i + 1}" max="{i + 1}" width="{width}" customWidth="1"/>')
    return f"<cols>{''.join(cols)}</cols>"


def _sheet_xml(sheet: Sheet) -> tuple[str, list[tuple[str, str]]]:
    """The worksheet XML and its (cell ref, url) hyperlinks."""
    header = "".join(_cell(f"{column_letter(i)}1", h, 1) for i, h in enumerate(sheet.headers))
    rows = [f'<row r="1">{header}</row>']
    links: list[tuple[str, str]] = []
    for r, values in enumerate(sheet.rows, start=2):
        cells = []
        for c, value in enumerate(values):
            ref = f"{column_letter(c)}{r}"
            link = c in sheet.link_columns and isinstance(value, str) and value.startswith("https://")
            cells.append(_cell(ref, value, 2 if link else 0))
            if link:
                links.append((ref, str(value)))
        rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    hyperlinks = "".join(
        f'<hyperlink ref="{ref}" r:id="rId{n}"/>' for n, (ref, _) in enumerate(links, start=1)
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
        'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        f"{_widths(sheet)}<sheetData>{''.join(rows)}</sheetData>"
        + (f"<hyperlinks>{hyperlinks}</hyperlinks>" if links else "")
        + "</worksheet>"
    )
    return xml, links


def _sheet_rels(links: list[tuple[str, str]]) -> str:
    rels = "".join(
        f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        f'relationships/hyperlink" Target="{_clean(url)}" TargetMode="External"/>'
        for n, (_, url) in enumerate(links, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels}</Relationships>"
    )


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font>'
    '<font><u/><sz val="11"/><color rgb="FF0563C1"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border/></borders>'
    '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
    '<cellXfs count="3"><xf fontId="0"/><xf fontId="1" applyFont="1"/>'
    '<xf fontId="2" applyFont="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def _sheet_names(sheets: Sequence[Sheet]) -> list[str]:
    names: list[str] = []
    for sheet in sheets:
        base = re.sub(r"[\\/?*\[\]:]", " ", sheet.name)[:_MAX_SHEET_NAME] or "Sheet"
        name, n = base, 2
        while name.lower() in (x.lower() for x in names):
            name = f"{base[: _MAX_SHEET_NAME - 3]} {n}"
            n += 1
        names.append(name)
    return names


def write_workbook(path: str, sheets: Sequence[Sheet]) -> None:
    names = _sheet_names(sheets)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        overrides = "".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(1, len(sheets) + 1)
        )
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            f"{overrides}</Types>",
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
        sheet_entries = "".join(
            f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
            for i, name in enumerate(names, start=1)
        )
        z.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{sheet_entries}</sheets></workbook>",
        )
        book_rels = "".join(
            f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/'
            f'2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(sheets) + 1)
        )
        book_rels += (
            f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{book_rels}</Relationships>",
        )
        z.writestr("xl/styles.xml", _STYLES)
        for i, sheet in enumerate(sheets, start=1):
            xml, links = _sheet_xml(sheet)
            z.writestr(f"xl/worksheets/sheet{i}.xml", xml)
            if links:
                z.writestr(f"xl/worksheets/_rels/sheet{i}.xml.rels", _sheet_rels(links))

from __future__ import annotations

import math
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape

import pandas as pd


@dataclass(frozen=True)
class ExportColumn:
    key: str
    header: str
    kind: str = "text"
    percent_scale: str | None = None


EXPORT_COLUMNS = [
    ExportColumn("extracted_at", "分析日時", "datetime"),
    ExportColumn("edinet_code", "EDINETコード"),
    ExportColumn("sec_code", "証券コード", "security_code"),
    ExportColumn("company_name", "企業名"),
    ExportColumn("fiscal_year", "決算年度"),
    ExportColumn("period_end", "決算期末日", "date"),
    ExportColumn("revenue", "売上高（円）", "integer"),
    ExportColumn("operating_income", "営業利益（円）", "integer"),
    ExportColumn("net_income", "当期純利益（円）", "integer"),
    ExportColumn("total_assets", "総資産（円）", "integer"),
    ExportColumn("equity_for_export", "自己資本（円）", "integer"),
    ExportColumn("interest_bearing_debt", "有利子負債（円）", "integer"),
    ExportColumn("cash_and_equivalents", "現金及び現金同等物（円）", "integer"),
    ExportColumn("roe", "ROE", "percent", "points"),
    ExportColumn("roa", "ROA", "percent", "points"),
    ExportColumn("roic", "ROIC", "percent", "points"),
    ExportColumn("roic_spread", "ROICスプレッド", "percent", "points"),
    ExportColumn("per", "PER", "decimal"),
    ExportColumn("pbr", "PBR", "decimal"),
    ExportColumn("eps", "EPS（円）", "decimal"),
    ExportColumn("bps", "BPS（円）", "decimal"),
    ExportColumn("operating_margin", "営業利益率", "percent", "points"),
    ExportColumn("equity_ratio", "自己資本比率", "percent", "points"),
    ExportColumn("interest_bearing_debt_ratio", "有利子負債比率", "percent", "points"),
    ExportColumn("dcf_theoretical_share_price", "DCF理論株価（円）", "decimal"),
    ExportColumn("current_share_price", "現在株価（円）", "decimal"),
    ExportColumn("upside_rate", "上昇余地", "percent", "ratio"),
    ExportColumn("wacc", "WACC", "percent", "ratio"),
    ExportColumn("perpetual_growth_rate", "永続成長率", "percent", "ratio"),
]


def build_analysis_history_export_table(history: pd.DataFrame) -> pd.DataFrame:
    data = history.copy()
    for column in [spec.key for spec in EXPORT_COLUMNS]:
        if column not in data.columns:
            data[column] = None

    if data.empty:
        return pd.DataFrame(columns=[spec.header for spec in EXPORT_COLUMNS])

    data = _sort_history_newest_first(data)
    data["equity_for_export"] = _first_available(data, ["equity", "net_assets"])

    shares = _first_available(data, ["shares_for_valuation"])
    issued = _numeric_series(data, "issued_shares")
    treasury = _numeric_series(data, "treasury_shares")
    calculated_shares = issued - treasury
    shares = shares.combine_first(calculated_shares.where(calculated_shares > 0))

    if "eps" not in history.columns or data["eps"].dropna().empty:
        data["eps"] = _safe_divide(_numeric_series(data, "net_income"), shares)
    if "bps" not in history.columns or data["bps"].dropna().empty:
        data["bps"] = _safe_divide(_numeric_series(data, "equity_for_export"), shares)

    if "per" not in history.columns or data["per"].dropna().empty:
        data["per"] = _safe_divide(_numeric_series(data, "current_share_price"), _numeric_series(data, "eps"))
    if "pbr" not in history.columns or data["pbr"].dropna().empty:
        data["pbr"] = _safe_divide(_numeric_series(data, "current_share_price"), _numeric_series(data, "bps"))

    if ("roic_spread" not in history.columns or data["roic_spread"].dropna().empty) and "wacc" in data.columns:
        roic = _numeric_series(data, "roic")
        wacc_points = _normalise_percent_series(_numeric_series(data, "wacc"), scale="ratio") * 100
        data["roic_spread"] = roic - wacc_points

    table = pd.DataFrame()
    for spec in EXPORT_COLUMNS:
        table[spec.header] = data[spec.key].map(lambda value, column_spec=spec: _export_value(value, column_spec))
    return table


def create_analysis_history_excel(history: pd.DataFrame) -> bytes:
    table = build_analysis_history_export_table(history)
    rows = table.values.tolist()
    headers = table.columns.tolist()
    column_specs = EXPORT_COLUMNS

    workbook = BytesIO()
    with zipfile.ZipFile(workbook, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml())
        archive.writestr("_rels/.rels", _root_rels_xml())
        archive.writestr("docProps/app.xml", _app_xml())
        archive.writestr("docProps/core.xml", _core_xml())
        archive.writestr("xl/workbook.xml", _workbook_xml())
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_rels_xml())
        archive.writestr("xl/styles.xml", _styles_xml())
        archive.writestr("xl/worksheets/sheet1.xml", _worksheet_xml(headers, rows, column_specs))
    return workbook.getvalue()


def _sort_history_newest_first(data: pd.DataFrame) -> pd.DataFrame:
    sort_frame = data.copy()
    sort_columns: list[str] = []
    for source, helper in [
        ("extracted_at", "_sort_extracted_at"),
        ("submit_date_time", "_sort_submit_date_time"),
        ("period_end", "_sort_period_end"),
        ("id", "_sort_id"),
    ]:
        if source in sort_frame.columns:
            if source == "id":
                sort_frame[helper] = pd.to_numeric(sort_frame[source], errors="coerce")
            else:
                sort_frame[helper] = pd.to_datetime(sort_frame[source], errors="coerce")
            sort_columns.append(helper)
    if not sort_columns:
        return data.reset_index(drop=True)
    return sort_frame.sort_values(sort_columns, ascending=False, na_position="last").drop(columns=sort_columns).reset_index(drop=True)


def _first_available(data: pd.DataFrame, columns: list[str]) -> pd.Series:
    result = pd.Series(math.nan, index=data.index, dtype="float64")
    for column in columns:
        if column in data.columns:
            result = result.combine_first(_numeric_series(data, column))
    return result


def _numeric_series(data: pd.DataFrame, column: str) -> pd.Series:
    if column not in data.columns:
        return pd.Series(math.nan, index=data.index, dtype="float64")
    return pd.to_numeric(data[column], errors="coerce")


def _safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denominator = denominator.mask(denominator == 0)
    return numerator / denominator


def _normalise_percent_series(values: pd.Series, scale: str | None) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    if scale == "points":
        return numeric / 100
    if scale == "ratio":
        return numeric.where(numeric.abs() <= 1, numeric / 100)
    return numeric


def _export_value(value: object, spec: ExportColumn) -> object:
    if _is_missing(value):
        return None
    if spec.kind == "date":
        return _parse_date(value)
    if spec.kind == "datetime":
        return _parse_datetime(value)
    if spec.kind == "percent":
        numeric = _to_float(value)
        if numeric is None:
            return None
        if spec.percent_scale == "points":
            return numeric / 100
        if spec.percent_scale == "ratio" and abs(numeric) > 1:
            return numeric / 100
        return numeric
    if spec.kind in {"integer", "decimal"}:
        return _to_float(value)
    if spec.kind == "security_code":
        return format_security_code(value)
    return str(value)


def _worksheet_xml(headers: list[str], rows: list[list[object]], column_specs: list[ExportColumn]) -> str:
    total_rows = len(rows) + 1
    total_columns = len(headers)
    end_cell = f"{_column_letter(total_columns)}{max(total_rows, 1)}"
    widths = _column_widths(headers, rows, column_specs)

    column_xml = "".join(
        f'<col min="{index}" max="{index}" width="{width:.1f}" customWidth="1"/>'
        for index, width in enumerate(widths, start=1)
    )
    sheet_rows = [_row_xml(1, headers, [ExportColumn("", "", "header") for _ in headers])]
    for row_index, row_values in enumerate(rows, start=2):
        sheet_rows.append(_row_xml(row_index, row_values, column_specs))

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<dimension ref="A1:{end_cell}"/>'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft"/>'
        '</sheetView></sheetViews>'
        f"<cols>{column_xml}</cols>"
        f"<sheetData>{''.join(sheet_rows)}</sheetData>"
        f'<autoFilter ref="A1:{end_cell}"/>'
        '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
        "</worksheet>"
    )


def _row_xml(row_number: int, values: list[object], specs: list[ExportColumn]) -> str:
    cells = []
    for column_index, (value, spec) in enumerate(zip(values, specs), start=1):
        cell_ref = f"{_column_letter(column_index)}{row_number}"
        cells.append(_cell_xml(cell_ref, value, spec))
    return f'<row r="{row_number}">{"".join(cells)}</row>'


def _cell_xml(cell_ref: str, value: object, spec: ExportColumn) -> str:
    if spec.kind == "header":
        return _inline_string_cell(cell_ref, str(value), style=1)
    if value is None:
        return ""

    style = _style_index(spec.kind)
    if isinstance(value, (date, datetime)):
        serial = _excel_date_serial(value)
        return f'<c r="{cell_ref}" s="{style}"><v>{serial:.10f}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            return ""
        return f'<c r="{cell_ref}" s="{style}"><v>{float(value):.15g}</v></c>'
    return _inline_string_cell(cell_ref, str(value), style=_style_index(spec.kind))


def _inline_string_cell(cell_ref: str, value: str, style: int) -> str:
    text = escape(value)
    return f'<c r="{cell_ref}" t="inlineStr" s="{style}"><is><t>{text}</t></is></c>'


def _style_index(kind: str) -> int:
    return {
        "datetime": 2,
        "date": 3,
        "integer": 4,
        "decimal": 5,
        "percent": 6,
        "security_code": 7,
    }.get(kind, 0)


def _column_widths(headers: list[str], rows: list[list[object]], column_specs: list[ExportColumn]) -> list[float]:
    widths: list[float] = []
    for index, header in enumerate(headers):
        sample_values = [row[index] for row in rows[:200] if index < len(row)]
        max_length = max([_display_length(header), *[_display_length(_display_value(value, column_specs[index])) for value in sample_values]])
        widths.append(min(max(max_length + 2, 10), 34))
    return widths


def _display_value(value: object, spec: ExportColumn) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        if spec.kind == "percent":
            return f"{float(value):.1%}"
        if spec.kind == "integer":
            return f"{float(value):,.0f}"
        if spec.kind == "decimal":
            return f"{float(value):,.2f}"
    return str(value)


def _display_length(value: object) -> int:
    text = str(value)
    return sum(2 if ord(char) > 0xFF else 1 for char in text)


def _column_letter(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, time())
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime().replace(tzinfo=None)


def _excel_date_serial(value: date | datetime) -> float:
    if isinstance(value, datetime):
        base = datetime(1899, 12, 30)
        return (value - base).total_seconds() / 86400
    return float((value - date(1899, 12, 30)).days)


def _to_float(value: object) -> float | None:
    if _is_missing(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_security_code(value: object) -> str | None:
    if _is_missing(value):
        return None

    text = str(value).strip()
    if not text:
        return None

    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    elif "." in text:
        integer_part, decimal_part = text.split(".", 1)
        if integer_part.isdigit() and set(decimal_part) <= {"0"}:
            text = integer_part

    if len(text) == 5 and text.isdigit() and text.endswith("0"):
        text = text[:-1]
    return text


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _content_types_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )


def _root_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        "</Relationships>"
    )


def _workbook_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheets>"
        '<sheet name="分析履歴" sheetId="1" r:id="rId1"/>'
        "</sheets>"
        "</workbook>"
    )


def _workbook_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )


def _styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="5">'
        '<numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm"/>'
        '<numFmt numFmtId="165" formatCode="yyyy-mm-dd"/>'
        '<numFmt numFmtId="166" formatCode="#,##0"/>'
        '<numFmt numFmtId="167" formatCode="#,##0.00"/>'
        '<numFmt numFmtId="168" formatCode="0.0%"/>'
        '</numFmts>'
        '<fonts count="2">'
        '<font><sz val="11"/><color theme="1"/><name val="Calibri"/><family val="2"/></font>'
        '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/><family val="2"/></font>'
        '</fonts>'
        '<fills count="3">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF2563EB"/><bgColor indexed="64"/></patternFill></fill>'
        '</fills>'
        '<borders count="2">'
        '<border><left/><right/><top/><bottom/><diagonal/></border>'
        '<border><left/><right/><top/><bottom style="thin"><color rgb="FFCBD5E1"/></bottom/><diagonal/></border>'
        '</borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="8">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"><alignment horizontal="center"/></xf>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="167" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="168" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '<dxfs count="0"/>'
        '<tableStyles count="0" defaultTableStyle="TableStyleMedium2" defaultPivotStyle="PivotStyleLight16"/>'
        '</styleSheet>'
    )


def _core_xml() -> str:
    created = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<dc:creator>EDINET Financial App</dc:creator>'
        '<cp:lastModifiedBy>EDINET Financial App</cp:lastModifiedBy>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{created}</dcterms:modified>'
        '</cp:coreProperties>'
    )


def _app_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        '<Application>EDINET Financial App</Application>'
        '</Properties>'
    )

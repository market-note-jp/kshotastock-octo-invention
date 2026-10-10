from __future__ import annotations

import math
import hashlib
import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from anomaly_detection import detect_anomalies
from app_config import get_user_data_dir, load_edinet_api_key
from database import get_saved_fiscal_month, save_financial_record
from edinet_api import EdinetApiError, EdinetClient, normalize_edinet_code
from excel_export import format_security_code
from financial_analysis import (
    INDICATOR_LABELS,
    RAW_METRIC_ORDER,
    calculate_indicators,
    calculate_price_indicators,
    format_amount,
    format_percent,
)
from report_generator import generate_analysis_comments
from xbrl_parser import extract_financial_data


DATA_DIR = get_user_data_dir()
DOWNLOAD_DIR = DATA_DIR / "downloads"
EXTRACT_DIR = DATA_DIR / "extracted"
MONTH_OPTIONS = tuple(range(1, 13))
SUBMISSION_MONTH_AUTO = "自動推定"
SUBMISSION_MONTH_ALL = "すべての月"


class AnalysisServiceError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def run_public_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    api_key = _resolve_api_key()
    if not api_key:
        raise AnalysisServiceError("EDINET APIキーが分析サーバーに設定されていません。", status_code=503)

    edinet_code = _clean_text(payload.get("edinetCode"))
    company_name = _clean_text(payload.get("companyKeyword"))
    if not edinet_code and not company_name:
        raise AnalysisServiceError("EDINETコードまたは企業名キーワードを入力してください。")
    if edinet_code and edinet_code.isdigit():
        raise AnalysisServiceError("EDINETコードは E02144 のように E から始まるコードを入力してください。")

    years = _parse_years(payload.get("years"))
    start_date = _parse_date(payload.get("startDate")) or (date.today().replace(year=date.today().year - years))
    end_date = _parse_date(payload.get("endDate")) or date.today()
    include_amendments = bool(payload.get("includeAmendments"))
    auto_analyze = bool(payload.get("autoAnalyze", True))
    manual_months = parse_submission_months(payload.get("submissionMonths"))

    fiscal_month = get_saved_fiscal_month(edinet_code, company_name)
    client = EdinetClient(api_key=api_key, cache_dir=DATA_DIR / "cache")
    search_result = client.search_annual_reports_smart(
        edinet_code=edinet_code,
        company_name=company_name,
        start_date=start_date,
        end_date=end_date,
        years=years,
        include_amendments=include_amendments,
        manual_months=manual_months,
        fiscal_month=fiscal_month,
        fiscal_month_source="saved" if fiscal_month is not None else None,
    )
    documents = [document.to_dict() for document in search_result.documents]

    response: dict[str, Any] = {
        "company": _company_payload(documents, edinet_code, company_name),
        "documents": [_document_payload(document) for document in documents],
        "searchNote": _search_note(search_result),
        "search": {
            "years": years,
            "includeAmendments": include_amendments,
            "autoAnalyze": auto_analyze,
            "fiscalMonth": search_result.fiscal_month,
            "submissionMonths": sorted(search_result.submission_months) if search_result.submission_months else None,
            "usedDiscovery": search_result.used_discovery,
            "usedFallback": search_result.used_fallback,
            "manualMonths": search_result.manual_months,
            "normalPeriodCount": search_result.normal_period_count,
        },
    }

    if auto_analyze and documents:
        analyzed_df = analyze_documents(client, documents)
        response["analysis"] = build_analysis_payload(analyzed_df)
    elif auto_analyze:
        response["analysis"] = None

    return _json_safe(response)


def parse_submission_months(raw_choices: object) -> set[int] | None:
    if not isinstance(raw_choices, list):
        return None
    choices = {str(choice) for choice in raw_choices}
    if SUBMISSION_MONTH_ALL in choices:
        return set(MONTH_OPTIONS)

    months: set[int] = set()
    for choice in choices:
        if not choice.endswith("月"):
            continue
        try:
            month = int(choice[:-1])
        except ValueError:
            continue
        if month in MONTH_OPTIONS:
            months.add(month)
    return months or None


def _parsed_document(client: EdinetClient, doc_id: str) -> dict[str, Any]:
    # The document ID identifies the filing; amendments have their own IDs.
    # Invalidate extraction caches whenever parser code or requested fields change.
    parser_version = hashlib.sha256(Path(__file__).with_name("xbrl_parser.py").read_bytes()).hexdigest()
    cache_key = hashlib.sha256(f"{doc_id}:{parser_version}".encode()).hexdigest()
    cache_dir = DATA_DIR / "parsed"
    cache_path = cache_dir / f"{cache_key}.json"
    try:
        parsed = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict) and parsed.get("facts_count", 0) > 0 and isinstance(parsed.get("metrics"), dict) and isinstance(parsed.get("details"), dict):
            return parsed
    except (OSError, ValueError, TypeError):
        pass
    zip_path = client.download_document_zip(doc_id, DOWNLOAD_DIR)
    extracted_dir = client.extract_zip(zip_path, EXTRACT_DIR)
    parsed = extract_financial_data(extracted_dir)
    if parsed.get("facts_count", 0) > 0:
        cache_dir.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache_dir, delete=False) as handle:
                temporary_path = Path(handle.name)
                json.dump(parsed, handle, ensure_ascii=False)
            os.replace(temporary_path, cache_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
    return parsed


def analyze_documents(client: EdinetClient, documents: list[dict[str, Any]]) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    parsed_documents: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for document in documents:
        parsed = _parsed_document(client, str(document["doc_id"]))
        records.append(build_financial_record(document, parsed))
        parsed_documents.append((document, parsed))

    if not records:
        return pd.DataFrame()

    apply_latest_per_share_history(records, parsed_documents)

    analyzed_df = calculate_indicators(pd.DataFrame(records))
    for record in analyzed_df.to_dict(orient="records"):
        save_financial_record(record)
    return analyzed_df


def apply_latest_per_share_history(records: list[dict[str, Any]], parsed_documents: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
    latest_document, latest_parsed = max(parsed_documents, key=lambda pair: (pair[0].get("period_end") or "", pair[0].get("submit_date_time") or ""))
    history = latest_parsed.get("per_share_history", {})
    for record in records:
        for key in ("reported_eps", "reported_bps"):
            restated = history.get(record.get("period_end"), {}).get(key)
            if restated is not None:
                record[key] = restated
                record[key + "_source_doc_id"] = latest_document.get("doc_id")
                record["extraction_details_json"][key] = history[record["period_end"]].get(key + "_detail")


def build_financial_record(document: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    metrics = parsed.get("metrics", {})
    details = parsed.get("details", {})
    derived_period = _derive_period_from_details(details)
    period_start = document.get("period_start") or derived_period.get("start_date")
    period_end = document.get("period_end") or derived_period.get("end_date") or derived_period.get("instant")

    return {
        "edinet_code": document.get("edinet_code"),
        "company_name": document.get("filer_name"),
        "sec_code": format_security_code(document.get("sec_code")),
        "doc_id": document.get("doc_id"),
        "doc_type_code": document.get("doc_type_code"),
        "doc_description": document.get("doc_description"),
        "fiscal_year": _derive_fiscal_year(period_start, period_end),
        "period_start": period_start,
        "period_end": period_end,
        "submit_date_time": document.get("submit_date_time"),
        "unit": "円",
        **metrics,
        **parsed.get("document_metadata", {}),
        "source_updated_at": document.get("submit_date_time"),
        "record_type": "actual",
        "extraction_details_json": details,
        "raw_document_json": document.get("raw", {}),
    }


def build_analysis_payload(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {"records": [], "latest": None, "metrics": [], "anomalies": [], "comments": []}

    data = df.sort_values([column for column in ["period_end", "fiscal_year", "submit_date_time"] if column in df.columns])
    latest = data.iloc[-1]
    anomalies = detect_anomalies(data)
    return _json_safe({
        "profile": "financial-statements",
        "records": [_record_payload(record) for record in data.to_dict(orient="records")],
        "latest": {
            "companyName": latest.get("company_name"),
            "edinetCode": latest.get("edinet_code"),
            "secCode": format_security_code(latest.get("sec_code")),
            "periodEnd": latest.get("period_end"),
            "fiscalYear": latest.get("fiscal_year"),
        },
        "metrics": _metric_cards(latest),
        "priceIndicators": calculate_price_indicators(data),
        "anomalies": anomalies,
        "comments": generate_analysis_comments(data, anomalies),
        "financialBusinessDetected": any((_safe_number(latest.get(key)) or 0) > 0 for key in (
            "financial_services_receivables_current", "financial_services_receivables_noncurrent")),
    })


def _metric_cards(latest: pd.Series) -> list[dict[str, Any]]:
    return [
        {"key": "revenue", "label": "売上高", "value": format_amount(latest.get("revenue"), "百万円")},
        {"key": "operating_income", "label": "営業利益", "value": format_amount(latest.get("operating_income"), "百万円")},
        {"key": "roe", "label": "ROE", "value": format_percent(latest.get("roe"))},
        {"key": "roic", "label": "ROIC", "value": format_percent(latest.get("roic"))},
        {"key": "equity_ratio", "label": "自己資本比率", "value": format_percent(latest.get("equity_ratio"))},
        {"key": "fcf", "label": "FCF", "value": format_amount(latest.get("fcf"), "百万円")},
    ]


def _record_payload(record: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "edinet_code",
        "company_name",
        "sec_code",
        "doc_id",
        "doc_description",
        "fiscal_year",
        "period_end",
        "submit_date_time",
        "unit",
        *RAW_METRIC_ORDER,
        *INDICATOR_LABELS.keys(),
        "roic_tax_rate",
        "roic_tax_source",
        "roic_uses_estimated_tax",
        "roic_profit_base",
        "nopat",
        "invested_capital",
        "previous_invested_capital",
        "average_invested_capital",
        "roic_invested_capital_formula",
        "roic_uses_alternative_invested_capital",
        "roic_invested_capital_note",
        "roic_unavailable_reason",
        "reported_eps",
        "reported_bps",
        "reported_eps_source_doc_id",
        "reported_bps_source_doc_id",
        "roa_period_end",
        "roe_period_end",
        "accounting_standard",
        "consolidation_scope",
        "source_updated_at",
        "record_type",
        "issued_shares",
        "treasury_shares",
        "shares_for_valuation",
        "financial_services_receivables_current",
        "financial_services_receivables_noncurrent",
    ]
    payload = {key: record.get(key) for key in dict.fromkeys(keys)}
    payload["source_url"] = f"https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/{record['doc_id']}.pdf" if record.get("doc_id") else None
    payload["per_share_basis"] = "最新取得書類に再掲された公表値を優先。再掲値がない年度は当該年度の原資料を使用。"
    return payload


def _company_payload(documents: list[dict[str, Any]], edinet_code: str | None, company_name: str | None) -> dict[str, Any]:
    first = documents[0] if documents else {}
    return {
        "name": first.get("filer_name") or company_name or edinet_code,
        "edinetCode": first.get("edinet_code") or normalize_edinet_code(edinet_code),
        "secCode": format_security_code(first.get("sec_code")),
    }


def _document_payload(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "docId": document.get("doc_id"),
        "edinetCode": document.get("edinet_code"),
        "filerName": document.get("filer_name"),
        "docDescription": document.get("doc_description"),
        "docTypeCode": document.get("doc_type_code"),
        "submitDateTime": document.get("submit_date_time"),
        "periodEnd": document.get("period_end"),
        "xbrlFlag": document.get("xbrl_flag"),
    }


def _search_note(search_result: Any) -> str:
    notes: list[str] = []
    if search_result.fiscal_month is not None and search_result.submission_months and not search_result.manual_months:
        month_labels = "・".join(f"{month}月" for month in sorted(search_result.submission_months))
        notes.append(f"決算月を{search_result.fiscal_month}月と推定し、{month_labels}提出分を中心に検索しました。")
    elif search_result.manual_months and search_result.submission_months:
        month_labels = "・".join(f"{month}月" for month in sorted(search_result.submission_months))
        notes.append(f"指定された{month_labels}提出分を検索しました。")
    if search_result.used_fallback:
        notes.append("推定提出月だけでは必要件数に満たなかったため、全月検索に切り替えました。")
    return " ".join(notes)


def _resolve_api_key() -> str | None:
    return _clean_text(os.getenv("EDINET_API_KEY")) or load_edinet_api_key()


def _parse_years(value: object) -> int:
    try:
        years = int(value)
    except (TypeError, ValueError):
        years = 3
    if not 1 <= years <= 10:
        raise AnalysisServiceError("分析期間は1年から10年の間で指定してください。")
    return years


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise AnalysisServiceError("検索開始日または検索終了日の形式が正しくありません。") from exc


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _derive_period_from_details(details: dict[str, Any]) -> dict[str, Any]:
    for metric_key in ("revenue", "operating_cf", "pretax_income", "net_income", "total_assets"):
        detail = details.get(metric_key) or {}
        period = detail.get("context_period") or {}
        if period.get("start_date") or period.get("end_date") or period.get("instant"):
            return period
    return {}


def _derive_fiscal_year(period_start: str | None, period_end: str | None) -> str | None:
    if period_start and len(period_start) >= 4:
        return period_start[:4]
    if not period_end or len(period_end) < 7:
        return None
    try:
        year = int(period_end[:4])
        month = int(period_end[5:7])
    except ValueError:
        return period_end[:4]
    return str(year - 1 if month <= 3 else year)


def _dataframe_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    records = df.to_dict(orient="records") if isinstance(df, pd.DataFrame) and not df.empty else []
    return _json_safe(records)


def _safe_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return [_json_safe(item) for item in sorted(value)]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value

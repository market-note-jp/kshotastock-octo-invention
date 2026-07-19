from __future__ import annotations

import json
import logging
import os
import time
import unicodedata
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

import requests


BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"
ANNUAL_REPORT_DOC_TYPE = "120"
AMENDED_ANNUAL_REPORT_DOC_TYPE = "130"
ANNUAL_REPORT_DESCRIPTION = "有価証券報告書"
AMENDED_ANNUAL_REPORT_DESCRIPTION = "訂正有価証券報告書"
LOGGER = logging.getLogger(__name__)


class EdinetApiError(RuntimeError):
    """Raised when the EDINET API returns an error or an invalid payload."""


@dataclass(frozen=True)
class EdinetDocument:
    doc_id: str
    edinet_code: str | None
    sec_code: str | None
    filer_name: str | None
    doc_type_code: str | None
    doc_description: str | None
    submit_date_time: str | None
    period_start: str | None
    period_end: str | None
    xbrl_flag: str | None
    csv_flag: str | None
    raw: dict

    @classmethod
    def from_api(cls, payload: dict) -> "EdinetDocument":
        return cls(
            doc_id=payload.get("docID") or "",
            edinet_code=payload.get("edinetCode"),
            sec_code=payload.get("secCode"),
            filer_name=payload.get("filerName"),
            doc_type_code=payload.get("docTypeCode"),
            doc_description=payload.get("docDescription"),
            submit_date_time=payload.get("submitDateTime"),
            period_start=payload.get("periodStart"),
            period_end=payload.get("periodEnd"),
            xbrl_flag=payload.get("xbrlFlag"),
            csv_flag=payload.get("csvFlag"),
            raw=payload,
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SmartAnnualReportSearchResult:
    documents: list[EdinetDocument]
    fiscal_month: int | None
    submission_months: set[int] | None
    fiscal_month_source: str | None
    used_discovery: bool
    used_fallback: bool
    manual_months: bool
    normal_period_count: int


def default_date_range(years: int) -> tuple[date, date]:
    today = date.today()
    return today - timedelta(days=365 * years + 10), today


def estimate_submission_months(fiscal_month: int) -> set[int]:
    if fiscal_month not in range(1, 13):
        raise ValueError("決算月は1から12の間で指定してください。")
    return {((fiscal_month + offset - 1) % 12) + 1 for offset in (2, 3, 4)}


def derive_fiscal_month_from_period_end(period_end: str | None) -> int | None:
    if not period_end:
        return None
    try:
        return date.fromisoformat(str(period_end).strip()[:10]).month
    except (TypeError, ValueError):
        return None


def iter_dates(start_date: date, end_date: date, months: set[int] | None = None, reverse: bool = True) -> Iterable[date]:
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    total_days = (end_date - start_date).days + 1
    offsets = range(total_days - 1, -1, -1) if reverse else range(total_days)

    for offset in offsets:
        current = start_date + timedelta(days=offset)
        if months and current.month not in months:
            continue
        yield current


class EdinetClient:
    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: str | Path | None = None,
        request_delay: float | None = None,
        timeout: int = 30,
    ) -> None:
        self.api_key = api_key
        self.cache_dir = Path(cache_dir or os.getenv("EDINET_CACHE_DIR", "data/cache"))
        self.request_delay = float(
            request_delay if request_delay is not None else os.getenv("EDINET_REQUEST_DELAY", "0.1")
        )
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "edinet-financial-streamlit-mvp/0.1",
                "Accept": "application/json, application/octet-stream",
            }
        )

        if not self.api_key:
            raise EdinetApiError("EDINET APIキーを設定してください。")
        if self.api_key.strip() == "your_edinet_api_key_here":
            raise EdinetApiError("EDINET APIキーが初期値のままです。金融庁EDINETで取得したAPIキーを設定してください。")

    def _params(self, **params: object) -> dict:
        return {**params, "Subscription-Key": self.api_key}

    def _document_cache_path(self, target_date: date) -> Path:
        return self.cache_dir / "document_lists" / f"{target_date.isoformat()}.json"

    def list_documents(self, target_date: date, use_cache: bool = True) -> list[EdinetDocument]:
        cache_path = self._document_cache_path(target_date)
        if use_cache and cache_path.exists():
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                self._raise_for_metadata(payload)
                return [EdinetDocument.from_api(item) for item in payload.get("results", [])]
            except (json.JSONDecodeError, OSError, EdinetApiError):
                try:
                    cache_path.unlink(missing_ok=True)
                except OSError:
                    pass

        url = f"{BASE_URL}/documents.json"
        response = self.session.get(
            url,
            params=self._params(date=target_date.isoformat(), type=2),
            timeout=self.timeout,
        )
        self._raise_for_response(response)
        payload = response.json()
        self._raise_for_metadata(payload)

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(self.request_delay)

        return [EdinetDocument.from_api(item) for item in payload.get("results", [])]

    def test_connection(self, target_date: date | None = None) -> tuple[int, dict]:
        target_date = target_date or date.today()
        url = f"{BASE_URL}/documents.json"
        response = self.session.get(
            url,
            params=self._params(date=target_date.isoformat(), type=2),
            timeout=self.timeout,
        )
        self._raise_for_response(response)
        payload = response.json()
        self._raise_for_metadata(payload)
        return len(payload.get("results") or []), payload.get("metadata") or {}

    def search_annual_reports(
        self,
        edinet_code: str | None = None,
        company_name: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        months: set[int] | None = None,
        max_reports: int = 5,
        include_amendments: bool = False,
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[EdinetDocument]:
        if not edinet_code and not company_name:
            raise ValueError("EDINETコードまたは企業名を指定してください。")

        start_date = start_date or default_date_range(10)[0]
        end_date = end_date or default_date_range(10)[1]
        dates = list(iter_dates(start_date, end_date, months=months, reverse=True))
        results: list[EdinetDocument] = []
        seen_doc_ids: set[str] = set()
        normalized_code = normalize_edinet_code(edinet_code) if edinet_code else None
        normalized_name = unicodedata.normalize("NFKC", company_name).strip().lower() if company_name else None

        for index, target_date in enumerate(dates, start=1):
            documents = self.list_documents(target_date)
            for document in documents:
                if not document.doc_id:
                    continue
                if document.doc_id in seen_doc_ids:
                    continue
                if normalized_code and normalize_edinet_code(document.edinet_code) != normalized_code:
                    continue
                filer_name = unicodedata.normalize("NFKC", document.filer_name or "").lower()
                if normalized_name and normalized_name not in filer_name:
                    continue
                is_target_report, selection_reason = is_target_annual_report(document, include_amendments)
                if not is_target_report:
                    _log_document_filter_result(
                        include_amendments,
                        document,
                        accepted=False,
                        reason=selection_reason,
                    )
                    continue
                if str(document.xbrl_flag or "") != "1":
                    _log_document_filter_result(
                        include_amendments,
                        document,
                        accepted=False,
                        reason="XBRLフラグが1ではないため",
                    )
                    continue

                results.append(document)
                seen_doc_ids.add(document.doc_id)
                _log_document_filter_result(
                    include_amendments,
                    document,
                    accepted=True,
                    reason=selection_reason,
                )

            if progress_callback:
                progress_callback(index, len(dates), len(results))
            if len(results) >= max_reports:
                break

        return sorted(results[:max_reports], key=lambda item: item.submit_date_time or "", reverse=True)

    def search_annual_reports_smart(
        self,
        edinet_code: str | None = None,
        company_name: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        years: int = 5,
        include_amendments: bool = False,
        manual_months: set[int] | None = None,
        fiscal_month: int | None = None,
        fiscal_month_source: str | None = None,
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> SmartAnnualReportSearchResult:
        if not 1 <= years <= 10:
            raise ValueError("分析期間は1年から10年の間で指定してください。")
        if not edinet_code and not company_name:
            raise ValueError("EDINETコードまたは企業名を指定してください。")

        start_date = start_date or default_date_range(years)[0]
        end_date = end_date or default_date_range(years)[1]
        if start_date > end_date:
            start_date, end_date = end_date, start_date

        search_limit = years if not include_amendments else max(years * 2, years + 4)
        if manual_months is not None:
            normalized_months = {int(month) for month in manual_months if int(month) in range(1, 13)}
            if not normalized_months:
                raise ValueError("手動指定する提出月を1つ以上選択してください。")
            documents = self.search_annual_reports(
                edinet_code=edinet_code,
                company_name=company_name,
                start_date=start_date,
                end_date=end_date,
                months=normalized_months,
                max_reports=search_limit,
                include_amendments=include_amendments,
                progress_callback=progress_callback,
            )
            selected = select_annual_reports_for_analysis(documents, years, include_amendments)
            return SmartAnnualReportSearchResult(
                documents=selected,
                fiscal_month=fiscal_month,
                submission_months=normalized_months,
                fiscal_month_source=fiscal_month_source,
                used_discovery=False,
                used_fallback=False,
                manual_months=True,
                normal_period_count=count_normal_annual_report_periods(selected),
            )

        discovery_documents: list[EdinetDocument] = []
        used_discovery = fiscal_month is None
        if fiscal_month is None:
            discovery_year = start_date.year
            discovery_documents = self.search_annual_reports(
                edinet_code=edinet_code,
                company_name=company_name,
                start_date=date(discovery_year, 1, 1),
                end_date=date(discovery_year, 12, 31),
                months=None,
                max_reports=3 if include_amendments else 1,
                include_amendments=include_amendments,
                progress_callback=progress_callback,
            )
            for document in discovery_documents:
                fiscal_month = derive_fiscal_month_from_period_end(document.period_end)
                if fiscal_month is not None:
                    fiscal_month_source = "discovery"
                    break

        submission_months = estimate_submission_months(fiscal_month) if fiscal_month is not None else None
        in_range_discovery = [
            document
            for document in discovery_documents
            if _document_submitted_in_range(document, start_date, end_date)
        ]
        documents = list(in_range_discovery)
        used_fallback = False

        if submission_months:
            estimated_documents = self.search_annual_reports(
                edinet_code=edinet_code,
                company_name=company_name,
                start_date=start_date,
                end_date=end_date,
                months=submission_months,
                max_reports=search_limit,
                include_amendments=include_amendments,
                progress_callback=progress_callback,
            )
            documents = deduplicate_documents([*documents, *estimated_documents])

        if not submission_months or count_normal_annual_report_periods(documents) < years:
            used_fallback = True
            all_month_documents = self.search_annual_reports(
                edinet_code=edinet_code,
                company_name=company_name,
                start_date=start_date,
                end_date=end_date,
                months=None,
                max_reports=search_limit,
                include_amendments=include_amendments,
                progress_callback=progress_callback,
            )
            documents = deduplicate_documents([*documents, *all_month_documents])

        selected = select_annual_reports_for_analysis(documents, years, include_amendments)
        return SmartAnnualReportSearchResult(
            documents=selected,
            fiscal_month=fiscal_month,
            submission_months=submission_months,
            fiscal_month_source=fiscal_month_source,
            used_discovery=used_discovery,
            used_fallback=used_fallback,
            manual_months=False,
            normal_period_count=count_normal_annual_report_periods(selected),
        )

    def search_documents_by_edinet_code(
        self,
        edinet_code: str,
        start_date: date,
        end_date: date,
        months: set[int] | None = None,
        max_documents: int = 50,
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[EdinetDocument]:
        normalized_code = normalize_edinet_code(edinet_code)
        dates = list(iter_dates(start_date, end_date, months=months, reverse=True))
        results: list[EdinetDocument] = []
        seen_doc_ids: set[str] = set()

        for index, target_date in enumerate(dates, start=1):
            documents = self.list_documents(target_date)
            for document in documents:
                if not document.doc_id or document.doc_id in seen_doc_ids:
                    continue
                if normalize_edinet_code(document.edinet_code) != normalized_code:
                    continue
                results.append(document)
                seen_doc_ids.add(document.doc_id)

            if progress_callback:
                progress_callback(index, len(dates), len(results))
            if len(results) >= max_documents:
                break

        return sorted(results[:max_documents], key=lambda item: item.submit_date_time or "", reverse=True)

    def download_document_zip(self, doc_id: str, output_dir: str | Path, use_cache: bool = True) -> Path:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        zip_path = output_dir / f"{doc_id}.zip"
        if use_cache and zip_path.exists() and zip_path.stat().st_size > 0:
            return zip_path

        url = f"{BASE_URL}/documents/{doc_id}"
        response = self.session.get(url, params=self._params(type=1), timeout=self.timeout)
        self._raise_for_response(response)
        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type.lower():
            try:
                payload = response.json()
            except ValueError as exc:
                raise EdinetApiError(f"{doc_id} の取得に失敗しました。") from exc
            self._raise_for_metadata(payload)
            raise EdinetApiError(f"{doc_id} のZIP取得に失敗しました: {payload}")

        if not response.content.startswith(b"PK"):
            raise EdinetApiError(f"{doc_id} のレスポンスがZIP形式ではありません。")

        zip_path.write_bytes(response.content)
        time.sleep(self.request_delay)
        return zip_path

    def extract_zip(self, zip_path: str | Path, output_root: str | Path) -> Path:
        zip_path = Path(zip_path)
        extract_dir = Path(output_root) / zip_path.stem
        extract_dir.mkdir(parents=True, exist_ok=True)
        root = extract_dir.resolve()

        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                target = (extract_dir / member.filename).resolve()
                if root != target and root not in target.parents:
                    raise EdinetApiError(f"ZIP内に不正なパスが含まれています: {member.filename}")
                archive.extract(member, extract_dir)

        return extract_dir

    @staticmethod
    def _raise_for_response(response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise EdinetApiError(f"EDINET APIのHTTPエラー: {response.status_code}") from exc

    @staticmethod
    def _raise_for_metadata(payload: dict) -> None:
        if "StatusCode" in payload:
            status = str(payload.get("StatusCode"))
            message = payload.get("message") or payload.get("Message") or payload
            if status in {"401", "403"}:
                raise EdinetApiError(
                    "EDINET APIキーが無効、または未有効化です。金融庁EDINETで取得した有効なAPIキーを設定してください。"
                )
            raise EdinetApiError(f"EDINET APIエラー: {status} {message}")

        metadata = payload.get("metadata") or {}
        status = str(metadata.get("status", "200"))
        if status != "200":
            message = metadata.get("message") or metadata.get("reason") or payload
            raise EdinetApiError(f"EDINET APIエラー: {status} {message}")


def parse_submit_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def normalize_edinet_code(value: str | None) -> str:
    if not value:
        return ""
    return unicodedata.normalize("NFKC", value).strip().replace(" ", "").upper()


def deduplicate_documents(documents: Iterable[EdinetDocument]) -> list[EdinetDocument]:
    unique_documents: dict[str, EdinetDocument] = {}
    for document in documents:
        if document.doc_id:
            unique_documents[document.doc_id] = document
    return sorted(
        unique_documents.values(),
        key=lambda item: item.submit_date_time or "",
        reverse=True,
    )


def count_normal_annual_report_periods(documents: Iterable[EdinetDocument]) -> int:
    periods = {
        document.period_end or document.doc_id
        for document in documents
        if _is_normal_annual_report(document)
    }
    return len(periods)


def select_annual_reports_for_analysis(
    documents: Iterable[EdinetDocument],
    years: int,
    include_amendments: bool,
) -> list[EdinetDocument]:
    unique_documents = deduplicate_documents(documents)
    selected_normal_ids: set[str] = set()
    selected_periods: set[str] = set()

    for document in unique_documents:
        if not _is_normal_annual_report(document):
            continue
        period_key = document.period_end or document.doc_id
        if period_key in selected_periods:
            continue
        selected_periods.add(period_key)
        selected_normal_ids.add(document.doc_id)
        if len(selected_periods) >= years:
            break

    selected: list[EdinetDocument] = []
    for document in unique_documents:
        if document.doc_id in selected_normal_ids:
            selected.append(document)
            continue
        if (
            include_amendments
            and _is_amended_annual_report(document)
            and document.period_end
            and document.period_end in selected_periods
        ):
            selected.append(document)
    return selected


def _is_normal_annual_report(document: EdinetDocument) -> bool:
    doc_type_code = str(document.doc_type_code or "").strip()
    description = unicodedata.normalize("NFKC", document.doc_description or "").strip()
    if "訂正" in description:
        return False
    if doc_type_code:
        return doc_type_code == ANNUAL_REPORT_DOC_TYPE
    return description.startswith(ANNUAL_REPORT_DESCRIPTION)


def _is_amended_annual_report(document: EdinetDocument) -> bool:
    doc_type_code = str(document.doc_type_code or "").strip()
    description = unicodedata.normalize("NFKC", document.doc_description or "").strip()
    return doc_type_code == AMENDED_ANNUAL_REPORT_DOC_TYPE or description.startswith(
        AMENDED_ANNUAL_REPORT_DESCRIPTION
    )


def _document_submitted_in_range(document: EdinetDocument, start_date: date, end_date: date) -> bool:
    submitted_at = parse_submit_date(document.submit_date_time)
    return submitted_at is not None and start_date <= submitted_at.date() <= end_date


def is_target_annual_report(document: EdinetDocument, include_amendments: bool) -> tuple[bool, str]:
    """Return whether an EDINET document is an annual report target and why."""
    doc_type_code = str(document.doc_type_code or "").strip()
    description = unicodedata.normalize("NFKC", document.doc_description or "").strip()
    is_amended_annual_description = description.startswith(AMENDED_ANNUAL_REPORT_DESCRIPTION)

    if "訂正" in description:
        if is_amended_annual_description:
            if include_amendments:
                return True, "docDescription が訂正有価証券報告書で始まり、include_amendments=True のため"
            return False, "訂正書類であり、include_amendments=False のため"
        return False, "訂正有価証券報告書以外の訂正書類のため"

    if doc_type_code:
        if doc_type_code == ANNUAL_REPORT_DOC_TYPE:
            return True, "docTypeCode=120 の有価証券報告書のため"
        if doc_type_code == AMENDED_ANNUAL_REPORT_DOC_TYPE:
            if include_amendments:
                return True, "docTypeCode=130 の訂正有価証券報告書であり、include_amendments=True のため"
            return False, "docTypeCode=130 の訂正有価証券報告書であり、include_amendments=False のため"
        return False, f"docTypeCode={doc_type_code} が取得対象外のため"

    if description.startswith(ANNUAL_REPORT_DESCRIPTION):
        return True, "docDescription が有価証券報告書で始まるため"

    if is_amended_annual_description:
        if include_amendments:
            return True, "docDescription が訂正有価証券報告書で始まり、include_amendments=True のため"
        return False, "訂正有価証券報告書であり、include_amendments=False のため"

    return False, "有価証券報告書または訂正有価証券報告書ではないため"


def _log_document_filter_result(
    include_amendments: bool,
    document: EdinetDocument,
    accepted: bool,
    reason: str,
) -> None:
    LOGGER.info(
        "include_amended=%s docID=%s docDescription=%s docTypeCode=%s 判定=%s 理由=%s",
        include_amendments,
        document.doc_id or "",
        document.doc_description or "",
        document.doc_type_code or "",
        "採用" if accepted else "除外",
        reason,
    )

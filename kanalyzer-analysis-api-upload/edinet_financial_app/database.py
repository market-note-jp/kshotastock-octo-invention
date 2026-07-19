from __future__ import annotations

import json
import os
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

import pandas as pd

from app_config import get_user_data_dir
from financial_analysis import INDICATOR_LABELS, RAW_METRIC_ORDER


DEFAULT_DB_FILENAME = "edinet_financials.sqlite3"
ROIC_TEXT_COLUMNS = [
    "roic_invested_capital_formula",
    "roic_invested_capital_note",
    "roic_unavailable_reason",
]
SHARE_METRIC_COLUMNS = [
    "issued_shares",
    "treasury_shares",
    "shares_for_valuation",
]
CAPEX_TEXT_COLUMNS = [
    "capex_method",
    "capex_source_tags",
]
CAPEX_FLAG_COLUMNS = [
    "capex_is_estimated",
]


def get_db_path() -> Path:
    override = os.getenv("EDINET_DB_PATH")
    if override:
        return Path(override)
    return get_user_data_dir() / DEFAULT_DB_FILENAME


def initialize_database(db_path: str | Path | None = None) -> Path:
    path = Path(db_path or get_db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        metric_columns = ",\n".join([f"{column} REAL" for column in RAW_METRIC_ORDER + list(INDICATOR_LABELS)])
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS financial_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                edinet_code TEXT,
                company_name TEXT,
                sec_code TEXT,
                doc_id TEXT UNIQUE,
                doc_type_code TEXT,
                doc_description TEXT,
                fiscal_year TEXT,
                period_start TEXT,
                period_end TEXT,
                submit_date_time TEXT,
                unit TEXT DEFAULT '円',
                {metric_columns},
                roic_tax_rate REAL,
                roic_tax_source TEXT,
                roic_uses_estimated_tax INTEGER,
                roic_profit_base TEXT,
                roic_invested_capital_formula TEXT,
                roic_uses_alternative_invested_capital INTEGER,
                roic_invested_capital_note TEXT,
                roic_unavailable_reason TEXT,
                nopat REAL,
                invested_capital REAL,
                extraction_details_json TEXT,
                raw_document_json TEXT,
                extracted_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_financial_data_edinet_code ON financial_data(edinet_code)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_financial_data_period_end ON financial_data(period_end)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS article_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_type TEXT,
                title TEXT NOT NULL,
                company_name TEXT,
                edinet_code TEXT,
                source_name TEXT,
                source_url TEXT,
                tags TEXT,
                body TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_article_entries_type ON article_entries(entry_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_article_entries_created_at ON article_entries(created_at)")
        _ensure_columns(conn, RAW_METRIC_ORDER + list(INDICATOR_LABELS) + SHARE_METRIC_COLUMNS, "REAL")
        _ensure_columns(conn, ROIC_TEXT_COLUMNS, "TEXT")
        _ensure_columns(conn, CAPEX_TEXT_COLUMNS, "TEXT")
        _ensure_columns(conn, CAPEX_FLAG_COLUMNS, "INTEGER")
        _ensure_columns(conn, ["roic_uses_alternative_invested_capital"], "INTEGER")
    return path


def save_financial_record(record: dict[str, Any], db_path: str | Path | None = None) -> None:
    path = initialize_database(db_path)
    columns = [
        "edinet_code",
        "company_name",
        "sec_code",
        "doc_id",
        "doc_type_code",
        "doc_description",
        "fiscal_year",
        "period_start",
        "period_end",
        "submit_date_time",
        "unit",
        *RAW_METRIC_ORDER,
        *SHARE_METRIC_COLUMNS,
        *CAPEX_TEXT_COLUMNS,
        *CAPEX_FLAG_COLUMNS,
        *INDICATOR_LABELS.keys(),
        "roic_tax_rate",
        "roic_tax_source",
        "roic_uses_estimated_tax",
        "roic_profit_base",
        "roic_invested_capital_formula",
        "roic_uses_alternative_invested_capital",
        "roic_invested_capital_note",
        "roic_unavailable_reason",
        "nopat",
        "invested_capital",
        "extraction_details_json",
        "raw_document_json",
    ]
    values = [_serialize_value(record.get(column)) for column in columns]
    placeholders = ", ".join(["?"] * len(columns))
    updates = ", ".join([f"{column}=excluded.{column}" for column in columns if column != "doc_id"])

    with sqlite3.connect(path) as conn:
        conn.execute(
            f"""
            INSERT INTO financial_data ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(doc_id) DO UPDATE SET {updates}
            """,
            values,
        )


def load_financial_data(edinet_code: str, db_path: str | Path | None = None) -> pd.DataFrame:
    path = initialize_database(db_path)
    with sqlite3.connect(path) as conn:
        return pd.read_sql_query(
            """
            SELECT *
            FROM financial_data
            WHERE UPPER(edinet_code) = UPPER(?)
            ORDER BY period_end, submit_date_time
            """,
            conn,
            params=(edinet_code,),
        )


def get_saved_fiscal_month(
    edinet_code: str | None = None,
    company_name: str | None = None,
    db_path: str | Path | None = None,
) -> int | None:
    path = initialize_database(db_path)
    with sqlite3.connect(path) as conn:
        if edinet_code:
            rows = conn.execute(
                """
                SELECT period_end
                FROM financial_data
                WHERE UPPER(edinet_code) = UPPER(?)
                  AND period_end IS NOT NULL
                  AND period_end != ''
                ORDER BY period_end DESC, submit_date_time DESC
                """,
                (edinet_code.strip(),),
            ).fetchall()
            fiscal_month = _latest_valid_fiscal_month(rows)
            if fiscal_month is not None:
                return fiscal_month

        normalized_name = unicodedata.normalize("NFKC", company_name or "").strip().lower()
        if normalized_name:
            rows = conn.execute(
                """
                SELECT company_name, period_end
                FROM financial_data
                WHERE company_name IS NOT NULL
                  AND period_end IS NOT NULL
                  AND period_end != ''
                ORDER BY period_end DESC, submit_date_time DESC
                """
            ).fetchall()
            matching_rows = [
                (period_end,)
                for saved_name, period_end in rows
                if normalized_name in unicodedata.normalize("NFKC", saved_name or "").lower()
            ]
            return _latest_valid_fiscal_month(matching_rows)
    return None


def load_analysis_history(db_path: str | Path | None = None) -> pd.DataFrame:
    path = initialize_database(db_path)
    with sqlite3.connect(path) as conn:
        return pd.read_sql_query(
            """
            SELECT *
            FROM financial_data
            ORDER BY
                datetime(extracted_at) DESC,
                datetime(submit_date_time) DESC,
                period_end DESC,
                id DESC
            """,
            conn,
        )


def delete_financial_data(edinet_code: str, db_path: str | Path | None = None) -> int:
    code = (edinet_code or "").strip()
    if not code:
        return 0

    path = initialize_database(db_path)
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            """
            DELETE FROM financial_data
            WHERE UPPER(edinet_code) = UPPER(?)
            """,
            (code,),
        )
        conn.commit()
        return cursor.rowcount


def list_saved_companies(db_path: str | Path | None = None) -> pd.DataFrame:
    path = initialize_database(db_path)
    with sqlite3.connect(path) as conn:
        data = pd.read_sql_query(
            """
            SELECT edinet_code, company_name, fiscal_year, period_end, submit_date_time
            FROM financial_data
            WHERE edinet_code IS NOT NULL
              AND edinet_code != ''
            ORDER BY edinet_code, period_end, submit_date_time
            """,
            conn,
        )

    columns = ["edinet_code", "company_name", "record_count", "oldest_period_end", "latest_period_end"]
    if data.empty:
        return pd.DataFrame(columns=columns)

    data["_submit_datetime_sort"] = pd.to_datetime(data["submit_date_time"], errors="coerce")
    data = data.sort_values(
        ["edinet_code", "period_end", "fiscal_year", "_submit_datetime_sort", "submit_date_time"],
        na_position="first",
    )
    deduped = data.drop_duplicates(subset=["edinet_code", "period_end", "fiscal_year"], keep="last")
    latest_names = (
        deduped.sort_values(["edinet_code", "period_end", "_submit_datetime_sort"], na_position="first")
        .groupby("edinet_code", as_index=False)
        .tail(1)[["edinet_code", "company_name"]]
    )
    summary = (
        deduped.groupby("edinet_code", as_index=False)
        .agg(
            record_count=("period_end", "count"),
            oldest_period_end=("period_end", "min"),
            latest_period_end=("period_end", "max"),
        )
        .merge(latest_names, on="edinet_code", how="left")
    )
    return summary[columns].sort_values("edinet_code").reset_index(drop=True)


def save_article_entry(entry: dict[str, Any], db_path: str | Path | None = None) -> int:
    title = str(entry.get("title") or "").strip()
    if not title:
        raise ValueError("タイトルを入力してください。")

    columns = [
        "entry_type",
        "title",
        "company_name",
        "edinet_code",
        "source_name",
        "source_url",
        "tags",
        "body",
    ]
    values = [_serialize_value(entry.get(column)) for column in columns]
    placeholders = ", ".join(["?"] * len(columns))

    path = initialize_database(db_path)
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            f"""
            INSERT INTO article_entries ({", ".join(columns)})
            VALUES ({placeholders})
            """,
            values,
        )
        conn.commit()
        return int(cursor.lastrowid)


def list_article_entries(db_path: str | Path | None = None) -> pd.DataFrame:
    path = initialize_database(db_path)
    with sqlite3.connect(path) as conn:
        return pd.read_sql_query(
            """
            SELECT id, entry_type, title, company_name, edinet_code, source_name,
                   source_url, tags, body, created_at, updated_at
            FROM article_entries
            ORDER BY datetime(created_at) DESC, id DESC
            """,
            conn,
        )


def delete_article_entry(entry_id: int, db_path: str | Path | None = None) -> int:
    path = initialize_database(db_path)
    with sqlite3.connect(path) as conn:
        cursor = conn.execute("DELETE FROM article_entries WHERE id = ?", (int(entry_id),))
        conn.commit()
        return cursor.rowcount


def _ensure_columns(conn: sqlite3.Connection, columns: list[str], column_type: str) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(financial_data)").fetchall()}
    for column in columns:
        if column not in existing:
            conn.execute(f"ALTER TABLE financial_data ADD COLUMN {column} {column_type}")


def _latest_valid_fiscal_month(rows: list[tuple]) -> int | None:
    for row in rows:
        try:
            parsed = pd.to_datetime(row[0], errors="coerce")
        except (TypeError, ValueError):
            continue
        if not pd.isna(parsed):
            return int(parsed.month)
    return None


def _serialize_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return int(value)
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    return value

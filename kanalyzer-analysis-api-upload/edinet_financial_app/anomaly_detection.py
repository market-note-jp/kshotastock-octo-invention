from __future__ import annotations

import math
from typing import Any

import pandas as pd


def detect_anomalies(df: pd.DataFrame) -> list[dict[str, str]]:
    if df.empty or len(df) < 2:
        return []

    data = df.sort_values([column for column in ["period_end", "fiscal_year"] if column in df.columns]).reset_index(drop=True)
    alerts: list[dict[str, str]] = []

    for index in range(1, len(data)):
        prev = data.iloc[index - 1]
        curr = data.iloc[index]
        year = _year_label(curr)

        revenue_growth = _pct_change(curr.get("revenue"), prev.get("revenue"))
        operating_cf_growth = _pct_change(curr.get("operating_cf"), prev.get("operating_cf"))
        net_income_growth = _pct_change(curr.get("net_income"), prev.get("net_income"))
        fcf_growth = _pct_change(curr.get("fcf"), prev.get("fcf"))

        if _positive(revenue_growth) and _negative(operating_cf_growth):
            alerts.append(
                _alert(
                    year,
                    "売上増加に対して営業CFが悪化",
                    "売上は増えていますが、営業キャッシュフローが前年より悪化しています。利益の質や運転資本の増加を確認してください。",
                    "warning",
                )
            )

        if _positive(net_income_growth) and _negative(fcf_growth):
            alerts.append(
                _alert(
                    year,
                    "利益増加に対してFCFが悪化",
                    "当期純利益は増えていますが、FCFが前年より悪化しています。投資負担や営業CFの動きを確認してください。",
                    "warning",
                )
            )

        inventory_growth = curr.get("inventory_growth_rate")
        if _valid(inventory_growth) and _valid(revenue_growth) and inventory_growth > revenue_growth + 20 and inventory_growth > 20:
            alerts.append(
                _alert(
                    year,
                    "棚卸資産の増加が売上成長を上回る",
                    "棚卸資産の増加率が売上成長率を大きく上回っています。在庫滞留や需要見通しの変化に注意が必要です。",
                    "warning",
                )
            )

        receivable_growth = curr.get("accounts_receivable_growth_rate")
        if _valid(receivable_growth) and _valid(revenue_growth) and receivable_growth > revenue_growth + 20 and receivable_growth > 20:
            alerts.append(
                _alert(
                    year,
                    "売掛金の増加が売上成長を上回る",
                    "売掛金の増加率が売上成長率を大きく上回っています。回収条件や与信管理を確認してください。",
                    "warning",
                )
            )

        operating_margin_delta = _delta(curr.get("operating_margin"), prev.get("operating_margin"))
        if _valid(operating_margin_delta) and operating_margin_delta <= -3:
            alerts.append(
                _alert(
                    year,
                    "営業利益率が大きく低下",
                    f"営業利益率が前年から{operating_margin_delta:.1f}ポイント低下しています。原価率や販管費の変化を確認してください。",
                    "warning",
                )
            )

        roic_delta = _delta(curr.get("roic"), prev.get("roic"))
        if _valid(roic_delta) and roic_delta <= -3:
            alerts.append(
                _alert(
                    year,
                    "ROICが大きく低下",
                    f"ROICが前年から{roic_delta:.1f}ポイント低下しています。収益性または投下資本の増加を確認してください。",
                    "warning",
                )
            )

        if _valid(curr.get("roe")) and _valid(curr.get("roic")) and curr.get("roe") >= 15 and curr.get("roic") <= 5:
            alerts.append(
                _alert(
                    year,
                    "ROEは高いがROICが低い",
                    "ROEは高い一方でROICが低く、財務レバレッジでROEが押し上げられている可能性があります。",
                    "info",
                )
            )

        equity_ratio_delta = _delta(curr.get("equity_ratio"), prev.get("equity_ratio"))
        if _valid(equity_ratio_delta) and equity_ratio_delta <= -5:
            alerts.append(
                _alert(
                    year,
                    "自己資本比率が大きく低下",
                    f"自己資本比率が前年から{equity_ratio_delta:.1f}ポイント低下しています。財務安全性の変化を確認してください。",
                    "warning",
                )
            )

        debt_ratio_delta = _delta(curr.get("interest_bearing_debt_ratio"), prev.get("interest_bearing_debt_ratio"))
        if _valid(debt_ratio_delta) and debt_ratio_delta >= 5:
            alerts.append(
                _alert(
                    year,
                    "有利子負債比率が大きく上昇",
                    f"有利子負債比率が前年から{debt_ratio_delta:.1f}ポイント上昇しています。借入増加や資本構成の変化を確認してください。",
                    "warning",
                )
            )

    return alerts


def _alert(year: str, title: str, message: str, severity: str) -> dict[str, str]:
    return {"year": year, "title": title, "message": message, "severity": severity}


def _year_label(row: pd.Series) -> str:
    for key in ("fiscal_year", "period_end", "submit_date_time"):
        value = row.get(key)
        if value is not None and not pd.isna(value):
            return str(value)[:10]
    return "対象年度"


def _pct_change(current: Any, previous: Any) -> float:
    if not _valid(current) or not _valid(previous) or float(previous) == 0:
        return math.nan
    return (float(current) - float(previous)) / abs(float(previous)) * 100


def _delta(current: Any, previous: Any) -> float:
    if not _valid(current) or not _valid(previous):
        return math.nan
    return float(current) - float(previous)


def _positive(value: Any) -> bool:
    return _valid(value) and float(value) > 0


def _negative(value: Any) -> bool:
    return _valid(value) and float(value) < 0


def _valid(value: Any) -> bool:
    if value is None:
        return False
    try:
        return not pd.isna(value)
    except TypeError:
        return False

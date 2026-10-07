from __future__ import annotations

import json
import math
from typing import Any

import pandas as pd

from xbrl_parser import METRIC_LABELS


RAW_METRIC_ORDER = [
    "revenue",
    "operating_income",
    "ordinary_income",
    "pretax_income",
    "income_taxes",
    "net_income",
    "total_assets",
    "net_assets",
    "equity",
    "operating_cf",
    "investing_cf",
    "financing_cf",
    "capex",
    "depreciation",
    "rnd_expenses",
    "inventories",
    "accounts_receivable",
    "accounts_payable",
    "interest_bearing_debt",
    "cash_and_equivalents",
]

INDICATOR_LABELS = {
    "sales_growth_rate": "売上成長率",
    "operating_margin": "営業利益率",
    "ordinary_margin": "経常利益率",
    "pretax_margin": "税引前利益率",
    "net_margin": "純利益率",
    "roe": "ROE",
    "roa": "ROA",
    "roic": "ROIC",
    "equity_ratio": "自己資本比率",
    "operating_cf_margin": "営業CFマージン",
    "fcf": "FCF",
    "accounts_receivable_growth_rate": "売掛金増加率",
    "inventory_growth_rate": "棚卸資産増加率",
    "interest_bearing_debt_ratio": "有利子負債比率",
}

MONETARY_COLUMNS = RAW_METRIC_ORDER + [
    "fcf",
    "nopat",
    "invested_capital",
    "previous_invested_capital",
    "average_invested_capital",
]
RATIO_COLUMNS = [
    "sales_growth_rate",
    "operating_margin",
    "ordinary_margin",
    "pretax_margin",
    "net_margin",
    "roe",
    "roa",
    "roic",
    "equity_ratio",
    "operating_cf_margin",
    "accounts_receivable_growth_rate",
    "inventory_growth_rate",
    "interest_bearing_debt_ratio",
    "roic_tax_rate",
]


def calculate_price_indicators(
    df: pd.DataFrame,
    current_share_price: float | None = None,
    manual_shares_outstanding: float | None = None,
) -> dict[str, Any]:
    if df.empty:
        return _empty_price_indicators()

    data = df.copy()
    sort_columns = [column for column in ["period_end", "fiscal_year", "submit_date_time"] if column in data.columns]
    if sort_columns:
        data = data.sort_values(sort_columns).reset_index(drop=True)
    latest = data.iloc[-1]

    issued_shares = _to_float(latest.get("issued_shares"))
    treasury_shares = _to_float(latest.get("treasury_shares"))
    shares_outstanding = _to_float(latest.get("shares_for_valuation"))
    share_count_source = "XBRL"
    if shares_outstanding is None and issued_shares is not None and treasury_shares is not None:
        shares_outstanding = issued_shares - treasury_shares
    if shares_outstanding is None or shares_outstanding <= 0:
        shares_outstanding = _to_float(manual_shares_outstanding)
        share_count_source = "手入力" if shares_outstanding and shares_outstanding > 0 else "取得不可"

    unit_name = str(latest.get("unit") or "円")
    net_income = _amount_to_yen(_to_float(latest.get("net_income")), unit_name)
    equity = _amount_to_yen(_to_float(latest.get("equity")), unit_name)
    if equity is None:
        equity = _amount_to_yen(_to_float(latest.get("net_assets")), unit_name)
    current_price = _to_float(current_share_price)

    eps = _safe_scalar_div(net_income, shares_outstanding)
    bps = _safe_scalar_div(equity, shares_outstanding)
    has_current_price = current_price is not None and current_price > 0
    per = _safe_scalar_div(current_price, eps) if has_current_price and eps is not None and eps > 0 else None
    pbr = _safe_scalar_div(current_price, bps) if has_current_price and bps is not None and bps > 0 else None

    warnings: list[str] = []
    if shares_outstanding is None or shares_outstanding <= 0:
        warnings.append("自己株式を除く評価用株式数をXBRLから確定できないため、原資料で確認した株式数を手入力してください。")
    if current_price is None or current_price <= 0:
        warnings.append("現在株価が未入力のため、PER/PBRは未計算です。")
    if net_income is None:
        warnings.append("当期純利益が取得できないため、EPS/PERは計算できません。")
    elif net_income <= 0:
        warnings.append("当期純利益が0以下のため、PERは計算不可です（赤字PER）。")
    if equity is None:
        warnings.append("自己資本が取得できないため、BPS/PBRは計算できません。")
    elif equity <= 0:
        warnings.append("自己資本が0以下のため、PBRは計算不可です。")

    return {
        "current_share_price": current_price,
        "issued_shares": issued_shares,
        "treasury_shares": treasury_shares,
        "shares_outstanding": shares_outstanding,
        "share_count_source": share_count_source,
        "eps": eps,
        "bps": bps,
        "per": per,
        "pbr": pbr,
        "net_income_yen": net_income,
        "equity_yen": equity,
        "unit": unit_name,
        "period_end": latest.get("period_end"),
        "fiscal_year": latest.get("fiscal_year"),
        "warnings": warnings,
    }


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    result = df.copy()
    for column in RAW_METRIC_ORDER:
        if column not in result.columns:
            result[column] = math.nan
        result[column] = pd.to_numeric(result[column], errors="coerce")

    sort_columns = [column for column in ["period_end", "fiscal_year", "submit_date_time"] if column in result.columns]
    if sort_columns:
        result = result.sort_values(sort_columns).reset_index(drop=True)

    equity = result["equity"].combine_first(result["net_assets"])
    avg_equity = _average_with_previous(equity)
    avg_assets = _average_with_previous(result["total_assets"])

    result["sales_growth_rate"] = result["revenue"].pct_change() * 100
    result["operating_margin"] = _safe_div(result["operating_income"], result["revenue"]) * 100
    result["ordinary_margin"] = _safe_div(result["ordinary_income"], result["revenue"]) * 100
    result["pretax_margin"] = _safe_div(result["pretax_income"], result["revenue"]) * 100
    result["net_margin"] = _safe_div(result["net_income"], result["revenue"]) * 100
    result["roe"] = _safe_div(result["net_income"], avg_equity) * 100
    result["roa"] = _safe_div(result["net_income"], avg_assets) * 100
    result["equity_ratio"] = _safe_div(equity, result["total_assets"]) * 100
    result["operating_cf_margin"] = _safe_div(result["operating_cf"], result["revenue"]) * 100
    result["fcf"] = result["operating_cf"] + result["investing_cf"]
    result["accounts_receivable_growth_rate"] = result["accounts_receivable"].pct_change() * 100
    result["inventory_growth_rate"] = result["inventories"].pct_change() * 100
    result["interest_bearing_debt_ratio"] = _safe_div(result["interest_bearing_debt"], result["total_assets"]) * 100

    roic_rows = [_calculate_roic_row(row) for _, row in result.iterrows()]
    result["roic_tax_rate"] = [row["tax_rate"] for row in roic_rows]
    result["roic_tax_source"] = [row["tax_source"] for row in roic_rows]
    result["roic_uses_estimated_tax"] = [row["uses_estimated_tax"] for row in roic_rows]
    result["roic_profit_base"] = [row["profit_base"] for row in roic_rows]
    result["nopat"] = [row["nopat"] for row in roic_rows]
    result["invested_capital"] = [row["invested_capital"] for row in roic_rows]
    result["previous_invested_capital"] = _previous_period_value(result, "invested_capital")
    result["average_invested_capital"] = (
        result["invested_capital"] + result["previous_invested_capital"]
    ) / 2
    result["average_invested_capital"] = result["average_invested_capital"].where(
        result["previous_invested_capital"].notna(),
        result["invested_capital"],
    )
    result["roic_invested_capital_formula"] = [row["invested_capital_formula"] for row in roic_rows]
    result["roic_uses_alternative_invested_capital"] = [
        row["uses_alternative_invested_capital"] for row in roic_rows
    ]
    result["roic_invested_capital_note"] = [row["invested_capital_note"] for row in roic_rows]
    result["roic_unavailable_reason"] = [row["unavailable_reason"] for row in roic_rows]
    result["roic"] = _safe_div(result["nopat"], result["average_invested_capital"]) * 100

    result = result.replace([math.inf, -math.inf], math.nan)
    return result


def raw_metric_label(metric_key: str) -> str:
    return METRIC_LABELS.get(metric_key, metric_key)


def indicator_label(metric_key: str) -> str:
    return INDICATOR_LABELS.get(metric_key, metric_key)


def display_label(metric_key: str) -> str:
    return raw_metric_label(metric_key) if metric_key in METRIC_LABELS else indicator_label(metric_key)


def unit_divisor(unit_name: str) -> float:
    return {"円": 1.0, "百万円": 1_000_000.0, "億円": 100_000_000.0}.get(unit_name, 1.0)


def format_percent(value: Any) -> str:
    if _is_missing(value):
        return "-"
    return f"{float(value):.1f}%"


def format_amount(value: Any, unit_name: str = "百万円") -> str:
    if _is_missing(value):
        return "-"
    return f"{float(value) / unit_divisor(unit_name):,.0f}{unit_name}"


def _calculate_roic_row(row: pd.Series) -> dict:
    pretax = _to_float(row.get("pretax_income"))
    taxes = _to_float(row.get("income_taxes"))
    operating_income = _to_float(row.get("operating_income"))

    tax_rate = math.nan
    tax_source = "実効税率"
    uses_estimated_tax = False
    if pretax not in (None, 0) and taxes is not None:
        candidate_rate = taxes / pretax
        if 0 <= candidate_rate <= 1:
            tax_rate = candidate_rate

    if _is_missing(tax_rate):
        tax_rate = 0.30
        tax_source = "暫定税率30%"
        uses_estimated_tax = True

    profit_base_value = operating_income
    profit_base_label = "営業利益"
    if profit_base_value is None and pretax is not None:
        profit_base_value = pretax
        profit_base_label = "税引前当期純利益（営業利益の代用）"

    nopat = profit_base_value * (1 - tax_rate) if profit_base_value is not None else math.nan

    debt = _to_float(row.get("interest_bearing_debt"))
    equity = _to_float(row.get("equity"))
    if equity is None:
        equity = _to_float(row.get("net_assets"))
    cash = _to_float(row.get("cash_and_equivalents"))
    total_assets = _to_float(row.get("total_assets"))
    accounts_payable = _to_float(row.get("accounts_payable"))

    invested_capital = math.nan
    invested_capital_formula = ""
    uses_alternative_invested_capital = False
    invested_capital_note = ""
    unavailable_reason = ""
    if debt is not None and equity is not None and cash is not None:
        invested_capital = debt + equity - cash
        invested_capital_note = _interest_bearing_debt_zero_note(row)
        invested_capital_formula = "有利子負債 + 自己資本 - 現金及び現金同等物"
    elif total_assets is not None and cash is not None and accounts_payable is not None:
        invested_capital = total_assets - cash - accounts_payable
        invested_capital_formula = "総資産 - 現金及び現金同等物 - 買掛金"
        uses_alternative_invested_capital = True
        if debt is None:
            invested_capital_note = "有利子負債が取得できないため、ROICは代替式で計算しています"
        else:
            invested_capital_note = "標準式の投下資本を構成する項目が不足しているため、ROICは代替式で計算しています"

    if _is_missing(invested_capital):
        unavailable_reason = "投下資本を構成する項目が不足しているため、ROICは計算できません"
    elif _is_missing(nopat):
        unavailable_reason = "ROIC計算に必要な利益データが不足しているため、ROICは計算できません"

    return {
        "tax_rate": tax_rate * 100 if tax_rate is not None else math.nan,
        "tax_source": tax_source,
        "uses_estimated_tax": uses_estimated_tax,
        "profit_base": profit_base_label,
        "nopat": nopat,
        "invested_capital": invested_capital,
        "invested_capital_formula": invested_capital_formula,
        "uses_alternative_invested_capital": uses_alternative_invested_capital,
        "invested_capital_note": invested_capital_note,
        "unavailable_reason": unavailable_reason,
    }


def _interest_bearing_debt_zero_note(row: pd.Series) -> str:
    detail = _metric_extraction_detail(row, "interest_bearing_debt")
    if isinstance(detail, dict) and detail.get("method") == "assumed_zero_no_debt_tags":
        return "有利子負債関連タグが見つからないため、0としてROICを計算しています"
    return ""


def _metric_extraction_detail(row: pd.Series, metric_key: str) -> dict | None:
    details = row.get("extraction_details_json")
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except json.JSONDecodeError:
            return None
    if not isinstance(details, dict):
        return None
    detail = details.get(metric_key)
    return detail if isinstance(detail, dict) else None


def _average_with_previous(series: pd.Series) -> pd.Series:
    previous = series.shift(1)
    averaged = (series + previous) / 2
    return averaged.where(previous.notna(), series)


def _previous_period_value(df: pd.DataFrame, value_column: str) -> pd.Series:
    period_keys = [column for column in ["period_end", "fiscal_year"] if column in df.columns]
    if not period_keys:
        return df[value_column].shift(1)

    period_values = df[[*period_keys, value_column]].groupby(period_keys, sort=False, dropna=False).last()
    period_values["previous_value"] = period_values[value_column].shift(1)
    previous_map = period_values["previous_value"].to_dict()
    return df.apply(lambda row: previous_map.get(tuple(row[key] for key in period_keys)), axis=1)


def _safe_div(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denominator = denominator.mask(denominator == 0)
    return numerator / denominator


def _safe_scalar_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _amount_to_yen(value: float | None, unit_name: str) -> float | None:
    if value is None:
        return None
    if unit_name == "百万円":
        return value * 1_000_000
    if unit_name == "億円":
        return value * 100_000_000
    return value


def _empty_price_indicators() -> dict[str, Any]:
    return {
        "current_share_price": None,
        "issued_shares": None,
        "treasury_shares": None,
        "shares_outstanding": None,
        "share_count_source": "取得不可",
        "eps": None,
        "bps": None,
        "per": None,
        "pbr": None,
        "net_income_yen": None,
        "equity_yen": None,
        "unit": "円",
        "period_end": None,
        "fiscal_year": None,
        "warnings": [],
    }


def _to_float(value: Any) -> float | None:
    if _is_missing(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except TypeError:
        return False

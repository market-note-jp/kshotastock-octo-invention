from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd


HISTORICAL_ASSUMPTIONS = {
    "sales_growth_rate": "売上成長率",
    "operating_margin": "営業利益率",
    "effective_tax_rate": "実効税率",
    "capex_rate": "CAPEX率",
    "depreciation_rate": "減価償却率",
    "working_capital_ratio": "運転資本率",
    "fcf_margin": "FCFマージン",
    "roic": "ROIC",
}


@dataclass
class DcfResult:
    forecast: pd.DataFrame
    enterprise_value: float
    terminal_value: float
    terminal_value_pv: float
    forecast_fcf_pv_sum: float
    equity_value: float
    theoretical_share_price: float
    price_difference: float
    upside_rate: float
    sensitivity: pd.DataFrame
    sensitivity_value_label: str


def calculate_historical_dcf_metrics(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    data = _prepare_data(df)
    row_count_before_dedupe = data.attrs.get("row_count_before_dedupe", len(data))
    row_count_after_dedupe = data.attrs.get("row_count_after_dedupe", len(data))
    if data.empty:
        return data, _empty_summary(), {
            "row_count_before_dedupe": row_count_before_dedupe,
            "row_count_after_dedupe": row_count_after_dedupe,
        }

    revenue = data["revenue"]
    operating_income = data["operating_income"]
    pretax_income = data["pretax_income"]
    income_taxes = data["income_taxes"]
    capex_outflow = data["capex"].abs()
    depreciation = data["depreciation"]
    working_capital = data["accounts_receivable"] + data["inventories"] - data["accounts_payable"]
    working_capital_ratio = _safe_div(working_capital, revenue)
    change_in_working_capital = working_capital.diff()
    tax_rate = _bounded_ratio(income_taxes, pretax_income, lower=0, upper=1)
    fcf_nopat = operating_income * (1 - tax_rate)
    roic_tax_rate = tax_rate.combine_first(data.get("roic_tax_rate", pd.Series(math.nan, index=data.index)) / 100)
    roic_tax_rate = roic_tax_rate.fillna(0.30)
    roic_profit_base = operating_income.combine_first(pretax_income)
    roic_nopat = roic_profit_base * (1 - roic_tax_rate)
    invested_capital = data.get("invested_capital", pd.Series(math.nan, index=data.index))
    fallback_invested_capital = data["total_assets"] - data["cash_and_equivalents"] - data["accounts_payable"]
    invested_capital = invested_capital.combine_first(fallback_invested_capital)
    previous_invested_capital = _previous_period_value(data.assign(_dcf_invested_capital=invested_capital), "_dcf_invested_capital")
    average_invested_capital = (invested_capital + previous_invested_capital) / 2
    average_invested_capital = average_invested_capital.where(previous_invested_capital.notna(), invested_capital)
    existing_fcf = data.get("fcf", pd.Series(math.nan, index=data.index))
    existing_roic = data.get("roic", pd.Series(math.nan, index=data.index))
    dcf_fcf = (fcf_nopat + depreciation - capex_outflow - change_in_working_capital).combine_first(existing_fcf)
    dcf_roic = _safe_div(roic_nopat, average_invested_capital).combine_first(existing_roic / 100)

    data["sales_growth_rate"] = revenue.pct_change()
    data["operating_margin"] = _safe_div(operating_income, revenue)
    data["effective_tax_rate"] = tax_rate
    data["capex_outflow"] = capex_outflow
    data["capex_rate"] = _safe_div(capex_outflow, revenue)
    data["depreciation_rate"] = _safe_div(depreciation, revenue)
    data["working_capital"] = working_capital
    data["working_capital_ratio"] = working_capital_ratio
    data["change_in_working_capital"] = change_in_working_capital
    data["dcf_nopat"] = roic_nopat
    data["dcf_fcf"] = dcf_fcf
    data["fcf_margin"] = _safe_div(dcf_fcf, revenue)
    data["dcf_invested_capital"] = invested_capital
    data["dcf_previous_invested_capital"] = previous_invested_capital
    data["dcf_average_invested_capital"] = average_invested_capital
    data["roic"] = dcf_roic

    summary_rows = []
    for key, label in HISTORICAL_ASSUMPTIONS.items():
        series = _clean_series(data.get(key, pd.Series(dtype=float)))
        summary_rows.append(
            {
                "key": key,
                "指標": label,
                "平均": series.mean() if not series.empty else math.nan,
                "中央値": series.median() if not series.empty else math.nan,
                "最小値": series.min() if not series.empty else math.nan,
                "最大値": series.max() if not series.empty else math.nan,
                "件数": int(series.count()),
            }
        )
    summary = pd.DataFrame(summary_rows)

    latest = data.sort_values([column for column in ["period_end", "fiscal_year"] if column in data.columns]).iloc[-1]
    latest_info = {
        "latest_revenue": _to_float(latest.get("revenue")),
        "latest_cash": _to_float(latest.get("cash_and_equivalents")),
        "latest_debt": _to_float(latest.get("interest_bearing_debt")),
        "latest_working_capital": _to_float(latest.get("working_capital")),
        "issued_shares": _to_float(latest.get("issued_shares")),
        "treasury_shares": _to_float(latest.get("treasury_shares")),
        "shares_for_valuation": _to_float(latest.get("shares_for_valuation")),
        "latest_period_end": latest.get("period_end"),
        "latest_fiscal_year": latest.get("fiscal_year"),
        "row_count_before_dedupe": row_count_before_dedupe,
        "row_count_after_dedupe": row_count_after_dedupe,
    }
    return data, summary, latest_info


def assumptions_from_summary(summary: pd.DataFrame) -> dict[str, float]:
    assumptions = {}
    for _, row in summary.iterrows():
        value = _to_float(row.get("平均"))
        assumptions[row["key"]] = value if value is not None else math.nan
    return assumptions


def build_dcf_model(
    latest_revenue: float,
    latest_cash: float,
    latest_debt: float,
    forecast_years: int,
    sales_growth_rate: float | list[float] | tuple[float, ...] | pd.Series,
    operating_margin: float | list[float] | tuple[float, ...] | pd.Series,
    effective_tax_rate: float,
    capex_rate: float | list[float] | tuple[float, ...] | pd.Series,
    depreciation_rate: float | list[float] | tuple[float, ...] | pd.Series,
    working_capital_ratio: float | list[float] | tuple[float, ...] | pd.Series,
    latest_working_capital: float,
    wacc: float,
    perpetual_growth_rate: float,
    shares_outstanding: float,
    current_share_price: float,
) -> DcfResult:
    if wacc <= perpetual_growth_rate:
        raise ValueError("WACCは永続成長率より大きい必要があります。")
    if forecast_years < 1:
        raise ValueError("予測年数は1年以上にしてください。")
    if latest_revenue is None or math.isnan(latest_revenue):
        raise ValueError("直近年度の売上高が取得できないため、DCFを計算できません。")
    if latest_working_capital is None or math.isnan(latest_working_capital):
        raise ValueError("直近期末の運転資本が取得できないため、DCFを計算できません。")

    sales_growth_rates = _expand_assumption(sales_growth_rate, forecast_years, "売上成長率")
    operating_margins = _expand_assumption(operating_margin, forecast_years, "営業利益率")
    capex_rates = _expand_assumption(capex_rate, forecast_years, "設備投資率")
    depreciation_rates = _expand_assumption(depreciation_rate, forecast_years, "減価償却率")
    working_capital_ratios = _expand_assumption(working_capital_ratio, forecast_years, "運転資本率")

    rows = []
    revenue = latest_revenue
    previous_working_capital = latest_working_capital
    for year in range(1, forecast_years + 1):
        index = year - 1
        revenue = revenue * (1 + sales_growth_rates[index])
        operating_income = revenue * operating_margins[index]
        nopat = operating_income * (1 - effective_tax_rate)
        depreciation = revenue * depreciation_rates[index]
        capex = revenue * capex_rates[index]
        projected_working_capital = revenue * working_capital_ratios[index]
        change_in_working_capital = projected_working_capital - previous_working_capital
        fcf = nopat + depreciation - capex - change_in_working_capital
        discount_factor = 1 / ((1 + wacc) ** year)
        discounted_fcf = fcf * discount_factor
        rows.append(
            {
                "year": year,
                "sales_growth_rate": sales_growth_rates[index],
                "operating_margin": operating_margins[index],
                "depreciation_rate": depreciation_rates[index],
                "capex_rate": capex_rates[index],
                "working_capital_ratio": working_capital_ratios[index],
                "revenue": revenue,
                "operating_income": operating_income,
                "nopat": nopat,
                "depreciation": depreciation,
                "capex": capex,
                "projected_working_capital": projected_working_capital,
                "previous_working_capital": previous_working_capital,
                "change_in_working_capital": change_in_working_capital,
                "fcf": fcf,
                "discount_factor": discount_factor,
                "discounted_fcf": discounted_fcf,
            }
        )
        previous_working_capital = projected_working_capital

    forecast = pd.DataFrame(rows)
    forecast_fcf_pv_sum = forecast["discounted_fcf"].sum()
    final_fcf = forecast.iloc[-1]["fcf"]
    terminal_value = final_fcf * (1 + perpetual_growth_rate) / (wacc - perpetual_growth_rate)
    terminal_value_pv = terminal_value / ((1 + wacc) ** forecast_years)
    enterprise_value = forecast_fcf_pv_sum + terminal_value_pv
    equity_value = enterprise_value + _zero_if_missing(latest_cash) - _zero_if_missing(latest_debt)

    theoretical_share_price = math.nan
    price_difference = math.nan
    upside_rate = math.nan
    if shares_outstanding and shares_outstanding > 0:
        theoretical_share_price = equity_value / shares_outstanding
        if current_share_price and current_share_price > 0:
            price_difference = theoretical_share_price - current_share_price
            upside_rate = price_difference / current_share_price

    sensitivity, sensitivity_value_label = build_sensitivity_table(
        forecast=forecast,
        latest_cash=latest_cash,
        latest_debt=latest_debt,
        shares_outstanding=shares_outstanding,
        base_wacc=wacc,
        base_growth=perpetual_growth_rate,
    )

    return DcfResult(
        forecast=forecast,
        enterprise_value=enterprise_value,
        terminal_value=terminal_value,
        terminal_value_pv=terminal_value_pv,
        forecast_fcf_pv_sum=forecast_fcf_pv_sum,
        equity_value=equity_value,
        theoretical_share_price=theoretical_share_price,
        price_difference=price_difference,
        upside_rate=upside_rate,
        sensitivity=sensitivity,
        sensitivity_value_label=sensitivity_value_label,
    )


def build_sensitivity_table(
    forecast: pd.DataFrame,
    latest_cash: float,
    latest_debt: float,
    shares_outstanding: float,
    base_wacc: float,
    base_growth: float,
) -> tuple[pd.DataFrame, str]:
    wacc_values = [0.06, 0.07, 0.08, 0.09, 0.10]
    growth_values = [0.00, 0.01, 0.02, 0.03]
    forecast_years = int(forecast["year"].max())
    final_fcf = forecast.iloc[-1]["fcf"]
    use_share_price = shares_outstanding and shares_outstanding > 0
    value_label = "理論株価"

    rows = []
    for growth in growth_values:
        row = {"永続成長率": growth}
        for wacc in wacc_values:
            column = f"WACC {wacc * 100:.1f}%"
            if wacc <= growth or not use_share_price:
                row[column] = math.nan
                continue
            fcf_pv_sum = (forecast["fcf"] / ((1 + wacc) ** forecast["year"])).sum()
            terminal_value = final_fcf * (1 + growth) / (wacc - growth)
            terminal_pv = terminal_value / ((1 + wacc) ** forecast_years)
            enterprise_value = fcf_pv_sum + terminal_pv
            equity_value = enterprise_value + _zero_if_missing(latest_cash) - _zero_if_missing(latest_debt)
            row[column] = equity_value / shares_outstanding
        rows.append(row)
    return pd.DataFrame(rows), value_label


def _prepare_data(df: pd.DataFrame) -> pd.DataFrame:
    data = df.copy()
    row_count_before_dedupe = len(data)
    numeric_columns = [
        "revenue",
        "operating_income",
        "pretax_income",
        "income_taxes",
        "capex",
        "depreciation",
        "accounts_receivable",
        "inventories",
        "accounts_payable",
        "interest_bearing_debt",
        "equity",
        "total_assets",
        "cash_and_equivalents",
        "invested_capital",
        "previous_invested_capital",
        "average_invested_capital",
        "roic_tax_rate",
        "fcf",
        "roic",
        "issued_shares",
        "treasury_shares",
        "shares_for_valuation",
    ]
    for column in numeric_columns:
        if column not in data.columns:
            data[column] = math.nan
        data[column] = pd.to_numeric(data[column], errors="coerce")

    data = _deduplicate_dcf_rows(data)
    row_count_after_dedupe = len(data)

    sort_columns = [column for column in ["period_end", "fiscal_year", "submit_date_time"] if column in data.columns]
    if sort_columns:
        data = data.sort_values(sort_columns).reset_index(drop=True)
    data.attrs["row_count_before_dedupe"] = row_count_before_dedupe
    data.attrs["row_count_after_dedupe"] = row_count_after_dedupe
    return data


def _deduplicate_dcf_rows(data: pd.DataFrame) -> pd.DataFrame:
    dedupe_keys = [column for column in ["edinet_code", "period_end", "fiscal_year"] if column in data.columns]
    if len(dedupe_keys) < 3 or data.empty:
        return data

    if "submit_date_time" not in data.columns:
        return data.drop_duplicates(subset=dedupe_keys, keep="last").copy()

    ranked = data.copy()
    ranked["_submit_datetime_sort"] = pd.to_datetime(ranked["submit_date_time"], errors="coerce")
    ranked = ranked.sort_values(
        [*dedupe_keys, "_submit_datetime_sort", "submit_date_time"],
        ascending=[True, True, True, False, False],
        na_position="last",
    )
    ranked = ranked.drop_duplicates(subset=dedupe_keys, keep="first")
    return ranked.drop(columns=["_submit_datetime_sort"])


def _previous_period_value(df: pd.DataFrame, value_column: str) -> pd.Series:
    period_keys = [column for column in ["period_end", "fiscal_year"] if column in df.columns]
    if not period_keys:
        return df[value_column].shift(1)

    period_values = df[[*period_keys, value_column]].groupby(period_keys, sort=False, dropna=False).last()
    period_values["previous_value"] = period_values[value_column].shift(1)
    previous_map = period_values["previous_value"].to_dict()
    return df.apply(lambda row: previous_map.get(tuple(row[key] for key in period_keys)), axis=1)


def _empty_summary() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "key": key,
                "指標": label,
                "平均": math.nan,
                "中央値": math.nan,
                "最小値": math.nan,
                "最大値": math.nan,
                "件数": 0,
            }
            for key, label in HISTORICAL_ASSUMPTIONS.items()
        ]
    )


def _expand_assumption(value: Any, forecast_years: int, label: str) -> list[float]:
    if isinstance(value, pd.Series):
        values = value.tolist()
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        values = [value] * forecast_years

    if len(values) != forecast_years:
        raise ValueError(f"{label}は{forecast_years}年分入力してください。")

    parsed_values: list[float] = []
    for raw_value in values:
        parsed = _to_float(raw_value)
        if parsed is None:
            raise ValueError(f"{label}に未入力または数値ではない値があります。")
        parsed_values.append(parsed)
    return parsed_values


def _bounded_ratio(numerator: pd.Series, denominator: pd.Series, lower: float, upper: float) -> pd.Series:
    ratio = _safe_div(numerator, denominator)
    return ratio.where((ratio >= lower) & (ratio <= upper))


def _safe_div(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    return numerator / denominator.mask(denominator == 0)


def _clean_series(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.replace([math.inf, -math.inf], math.nan).dropna()


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _zero_if_missing(value: Any) -> float:
    parsed = _to_float(value)
    return 0.0 if parsed is None else parsed

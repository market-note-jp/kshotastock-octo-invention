from __future__ import annotations

import math
from typing import Any

import pandas as pd


def generate_analysis_comments(df: pd.DataFrame, anomalies: list[dict[str, str]] | None = None) -> list[str]:
    if df.empty:
        return ["分析対象の財務データがありません。"]

    data = df.sort_values([column for column in ["period_end", "fiscal_year"] if column in df.columns]).reset_index(drop=True)
    latest = data.iloc[-1]
    previous = data.iloc[-2] if len(data) >= 2 else None
    comments: list[str] = []

    cagr = _cagr(data["revenue"].dropna())
    if cagr is not None:
        comments.append(f"売上高は過去{len(data)}期で年平均{cagr:.1f}%成長しています。")
    elif _valid(latest.get("revenue")):
        comments.append("売上高は取得できていますが、年平均成長率を計算するには過年度データが不足しています。")

    if previous is not None and _valid(latest.get("operating_cf")) and _valid(previous.get("operating_cf")):
        change = latest.get("operating_cf") - previous.get("operating_cf")
        direction = "改善" if change >= 0 else "悪化"
        comments.append(f"営業CFは前年から{direction}しています。利益とキャッシュフローの方向が一致しているか確認してください。")

    if _valid(latest.get("roic")):
        if previous is not None and _valid(previous.get("roic")):
            delta = latest.get("roic") - previous.get("roic")
            comments.append(f"ROICは{latest.get('roic'):.1f}%で、前年から{delta:.1f}ポイント変化しています。")
        else:
            comments.append(f"ROICは{latest.get('roic'):.1f}%です。投下資本に対する税引後利益の効率を確認できます。")

    if _valid(latest.get("roe")) and _valid(latest.get("roic")) and latest.get("roe") > latest.get("roic") + 5:
        comments.append("ROEがROICを大きく上回っています。財務レバレッジによってROEが押し上げられている可能性があります。")

    if _valid(latest.get("equity_ratio")):
        comments.append(f"自己資本比率は{latest.get('equity_ratio'):.1f}%です。財務安全性の確認に使えます。")

    if anomalies:
        first = anomalies[0]
        comments.append(f"異常検知では「{first['title']}」が検出されています。該当年度の注記やキャッシュフロー明細を確認してください。")

    return comments or ["主要指標の計算は完了しました。欠損項目が多い場合は、XBRLタグ候補の追加を検討してください。"]


def _cagr(series: pd.Series) -> float | None:
    values = [float(value) for value in series if _valid(value) and float(value) > 0]
    if len(values) < 2:
        return None
    periods = len(values) - 1
    return (values[-1] / values[0]) ** (1 / periods) * 100 - 100


def _valid(value: Any) -> bool:
    if value is None:
        return False
    try:
        return not pd.isna(value) and not math.isinf(float(value))
    except (TypeError, ValueError):
        return False

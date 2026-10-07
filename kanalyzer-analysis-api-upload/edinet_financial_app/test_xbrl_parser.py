from __future__ import annotations

import unittest

from xbrl_parser import ContextInfo, Fact, METRIC_SPECS, _extract_revenue


def _fact(name: str, value: float, context_ref: str, unit_ref: str = "JPY") -> Fact:
    return Fact(
        concept=f"test:{name}",
        local_name=name,
        value=value,
        context_ref=context_ref,
        unit_ref=unit_ref,
        source_file="test.xbrl",
    )


class RevenueExtractionTests(unittest.TestCase):
    def test_ifrs_net_sales_prefers_current_aggregate_context(self) -> None:
        contexts = {
            "CurrentYearDuration": ContextInfo("CurrentYearDuration", "2025-04-01", "2026-03-31"),
            "Prior1YearDuration": ContextInfo("Prior1YearDuration", "2024-04-01", "2025-03-31"),
            "CurrentYearDuration_SegmentMember": ContextInfo(
                "CurrentYearDuration_SegmentMember",
                "2025-04-01",
                "2026-03-31",
                raw_text="ReportableSegmentMember",
            ),
        }
        facts = [
            _fact("NetSalesIFRS", 12_479_620_000_000, "CurrentYearDuration"),
            _fact("NetSalesIFRS", 12_034_917_000_000, "Prior1YearDuration"),
            _fact("NetSalesIFRS", 4_685_651_000_000, "CurrentYearDuration_SegmentMember"),
        ]

        value, detail = _extract_revenue(METRIC_SPECS["revenue"], facts, contexts)

        self.assertEqual(value, 12_479_620_000_000)
        self.assertEqual(detail["local_name"], "NetSalesIFRS")
        self.assertEqual(detail["context_ref"], "CurrentYearDuration")
        statuses = {item["context_ref"]: item for item in detail["debug"]["candidates"]}
        self.assertEqual(statuses["CurrentYearDuration"]["status"], "adopted")
        self.assertIn("過年度context", statuses["Prior1YearDuration"]["reasons"])
        self.assertIn("セグメント・メンバー別context", statuses["CurrentYearDuration_SegmentMember"]["reasons"])

    def test_japanese_gaap_net_sales_is_still_supported(self) -> None:
        contexts = {
            "CurrentYearDuration": ContextInfo("CurrentYearDuration", "2025-04-01", "2026-03-31")
        }
        value, detail = _extract_revenue(
            METRIC_SPECS["revenue"],
            [_fact("NetSales", 500_000_000_000, "CurrentYearDuration")],
            contexts,
            prefer_consolidated=False,
        )
        self.assertEqual(value, 500_000_000_000)
        self.assertEqual(detail["debug"]["selected"]["local_name"], "NetSales")

    def test_unknown_revenue_tag_is_reported_when_not_selected(self) -> None:
        contexts = {
            "CurrentYearDuration": ContextInfo("CurrentYearDuration", "2025-04-01", "2026-03-31")
        }
        value, detail = _extract_revenue(
            METRIC_SPECS["revenue"],
            [_fact("RevenueCompanySpecificMetric", 123, "CurrentYearDuration")],
            contexts,
            prefer_consolidated=False,
        )
        self.assertIsNone(value)
        self.assertEqual(detail["method"], "not_found")
        candidate = detail["debug"]["candidates"][0]
        self.assertEqual(candidate["status"], "rejected")
        self.assertIn("売上高の候補タグ定義外", candidate["reasons"])


if __name__ == "__main__":
    unittest.main()

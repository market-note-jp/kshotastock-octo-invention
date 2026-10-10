from __future__ import annotations

import unittest

import pandas as pd

from analysis_service import AnalysisServiceError, _parse_years, parse_submission_months
from analysis_service import build_analysis_payload


class AnalysisServiceTest(unittest.TestCase):
    def test_parse_submission_months_keeps_auto_as_none(self) -> None:
        self.assertIsNone(parse_submission_months(["自動推定"]))

    def test_parse_submission_months_expands_all_months(self) -> None:
        self.assertEqual(parse_submission_months(["すべての月"]), set(range(1, 13)))

    def test_parse_submission_months_accepts_multiple_numbered_months(self) -> None:
        self.assertEqual(parse_submission_months(["1月", "3月", "12月"]), {1, 3, 12})

    def test_parse_years_validates_range(self) -> None:
        with self.assertRaises(AnalysisServiceError):
            _parse_years(11)

    def test_public_analysis_payload_does_not_include_heavy_xbrl_details(self) -> None:
        payload = build_analysis_payload(
            pd.DataFrame(
                [
                    {
                        "edinet_code": "E00001",
                        "company_name": "テスト株式会社",
                        "sec_code": "1234",
                        "doc_id": "S100TEST",
                        "doc_description": "有価証券報告書",
                        "fiscal_year": "2025",
                        "period_end": "2026-03-31",
                        "submit_date_time": "2026-06-30 10:00",
                        "unit": "円",
                        "revenue": 1000,
                        "operating_income": 100,
                        "ordinary_income": 100,
                        "pretax_income": 100,
                        "income_taxes": 30,
                        "net_income": 70,
                        "total_assets": 500,
                        "net_assets": 300,
                        "equity": 300,
                        "operating_cf": 90,
                        "investing_cf": -20,
                        "financing_cf": -10,
                        "capex": 20,
                        "depreciation": 10,
                        "inventories": 10,
                        "accounts_receivable": 10,
                        "accounts_payable": 5,
                        "interest_bearing_debt": 50,
                        "cash_and_equivalents": 80,
                        "roe": 23.3,
                        "roa": 14.0,
                        "roic": 20.0,
                        "equity_ratio": 60.0,
                        "fcf": 70,
                        "extraction_details_json": {"revenue": {"debug": ["large"]}},
                        "raw_document_json": {"debug": "large"},
                    }
                ]
            )
        )

        self.assertIn("records", payload)
        self.assertNotIn("dcf", payload)
        self.assertNotIn("extraction_details_json", payload["records"][0])
        self.assertNotIn("raw_document_json", payload["records"][0])


if __name__ == "__main__":
    unittest.main()

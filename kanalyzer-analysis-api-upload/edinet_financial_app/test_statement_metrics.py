import gc
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

import analysis_service as service
from database import save_financial_record, load_financial_data
from financial_analysis import calculate_indicators, calculate_price_indicators
from xbrl_parser import extract_financial_data, _extract_equity, ContextInfo, Fact


class StatementMetricTests(unittest.TestCase):
    def test_japanese_owners_equity_includes_accumulated_oci(self):
        contexts = {"CurrentYearInstant": ContextInfo("CurrentYearInstant", instant="2026-03-31")}
        facts = [Fact("test:"+name, name, value, "CurrentYearInstant", "JPY", "test.xbrl")
                 for name, value in (("ShareholdersEquity", 452489), ("ValuationAndTranslationAdjustments", 107876), ("NetAssets", 593193))]
        equity, detail = _extract_equity(facts, contexts)
        self.assertEqual(equity, 560365)
        self.assertEqual(detail["method"], "shareholders_equity_plus_accumulated_oci")

    def test_latest_filing_restatement_only_changes_matching_per_share_periods(self):
        records = [dict(period_end="2024-03-31", reported_eps=184.96, reported_bps=1236.73, extraction_details_json={}),
                   dict(period_end="2023-03-31", reported_eps=100, extraction_details_json={})]
        document = dict(doc_id="S100YGP3", period_end="2026-03-31", submit_date_time="2026-06-23")
        service.apply_latest_per_share_history(records, [(document, {"per_share_history":{"2024-03-31":{"reported_eps":30.83, "reported_bps":206.12}}})])
        self.assertEqual(records[0]["reported_eps"], 30.83)
        self.assertEqual(records[0]["reported_bps"], 206.12)
        self.assertEqual(records[0]["reported_eps_source_doc_id"], "S100YGP3")
        self.assertEqual(records[1]["reported_eps"], 100)

    def test_api_has_all_statement_columns_and_no_dcf(self):
        frame = calculate_indicators(pd.DataFrame([dict(
            period_end="2025-03-31", fiscal_year="2024", revenue=1000,
            gross_profit=300, operating_income=100, ordinary_income=90,
            net_income=70, pretax_income=100, income_taxes=30,
            equity=400, total_assets=800, capital_stock=40,
            interest_bearing_debt=100, cash_and_equivalents=50,
            operating_cf=90, investing_cf=-20, financing_cf=-10,
            depreciation=15, issued_shares=100, treasury_shares=None,
            shares_for_valuation=None, reported_eps=0.7, reported_bps=4,
            accounting_standard="日本会計基準", source_updated_at="2025-06-26", record_type="actual",
        )]))
        result = service.build_analysis_payload(frame)
        self.assertNotIn("dcf", result)
        row = result["records"][0]
        for key in ("gross_profit", "gross_margin", "ordinary_income", "ordinary_margin", "capital_stock", "reported_eps", "reported_bps", "financing_cf", "accounting_standard", "source_updated_at"):
            self.assertIsNotNone(row[key], key)
        self.assertEqual(row["gross_margin"], 30)
        self.assertEqual(row["ordinary_margin"], 9)
        self.assertEqual(row["fcf"], 70)
        self.assertEqual(row["roa_period_end"], 8.75)
        self.assertEqual(row["roe_period_end"], 17.5)
        self.assertAlmostEqual(row["roic"], 70 / 450 * 100)
        self.assertEqual(result["priceIndicators"]["issued_shares"], 100)
        self.assertIsNone(result["priceIndicators"]["shares_outstanding"])
        self.assertEqual(result["priceIndicators"]["eps"], .7)

    def test_parser_current_consolidated_per_share_fields_and_metadata(self):
        xml = '''<xbrl xmlns:x="urn:x" xmlns:xbrli="urn:xbrli">
        <xbrli:context id="CurrentYearDuration"><xbrli:period><xbrli:startDate>2024-04-01</xbrli:startDate><xbrli:endDate>2025-03-31</xbrli:endDate></xbrli:period></xbrli:context>
        <xbrli:context id="CurrentYearInstant"><xbrli:period><xbrli:instant>2025-03-31</xbrli:instant></xbrli:period></xbrli:context>
        <xbrli:context id="PriorYearDuration"><xbrli:period><xbrli:startDate>2023-04-01</xbrli:startDate><xbrli:endDate>2024-03-31</xbrli:endDate></xbrli:period></xbrli:context>
        <xbrli:context id="CurrentYearDuration_NonConsolidatedMember"><xbrli:period><xbrli:startDate>2024-04-01</xbrli:startDate><xbrli:endDate>2025-03-31</xbrli:endDate></xbrli:period></xbrli:context>
        <x:NetSales contextRef="CurrentYearDuration" unitRef="JPY">1000000000</x:NetSales>
        <x:GrossProfit contextRef="CurrentYearDuration" unitRef="JPY">300000000</x:GrossProfit>
        <x:CapitalStock contextRef="CurrentYearInstant" unitRef="JPY">50000000</x:CapitalStock>
        <x:NetIncomePerShareSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPYPerShares">94.93</x:NetIncomePerShareSummaryOfBusinessResults>
        <x:NetIncomePerShareSummaryOfBusinessResults contextRef="PriorYearDuration" unitRef="JPYPerShares">999</x:NetIncomePerShareSummaryOfBusinessResults>
        <x:NetIncomePerShareSummaryOfBusinessResults contextRef="CurrentYearDuration_NonConsolidatedMember" unitRef="JPYPerShares">3</x:NetIncomePerShareSummaryOfBusinessResults>
        <x:NetAssetsPerShareSummaryOfBusinessResults contextRef="CurrentYearInstant" unitRef="JPYPerShares">338.45</x:NetAssetsPerShareSummaryOfBusinessResults>
        <x:AccountingStandardsDEI contextRef="CurrentYearInstant">Japan GAAP</x:AccountingStandardsDEI>
        <x:WhetherConsolidatedFinancialStatementsArePreparedDEI contextRef="CurrentYearInstant">true</x:WhetherConsolidatedFinancialStatementsArePreparedDEI>
        </xbrl>'''
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "report.xbrl").write_text(xml, encoding="utf-8")
            with patch("xbrl_parser._extract_capex", side_effect=AssertionError("Unrequested extraction")):
                parsed = extract_financial_data(temp)
        self.assertEqual(parsed["metrics"]["reported_eps"], 94.93)
        self.assertEqual(parsed["metrics"]["reported_bps"], 338.45)
        self.assertEqual(parsed["metrics"]["gross_profit"], 300000000)
        self.assertEqual(parsed["metrics"]["capital_stock"], 50000000)
        self.assertNotIn("capex", parsed["metrics"])
        self.assertEqual(parsed["document_metadata"], {"accounting_standard":"日本会計基準", "consolidation_scope":"連結"})

    def test_per_share_public_values_allow_per_pbr_with_unknown_treasury(self):
        frame = pd.DataFrame([dict(reported_eps=10, reported_bps=100, issued_shares=1000, treasury_shares=None)])
        result = calculate_price_indicators(frame, current_share_price=200)
        self.assertEqual(result["per"], 20)
        self.assertEqual(result["pbr"], 2)
        self.assertEqual(result["eps_source"], "公表値")
        self.assertIsNone(result["shares_outstanding"])

    def test_manual_shares_override_public_values_and_automatic_share_count(self):
        frame = pd.DataFrame([dict(net_income=100, equity=500, issued_shares=1000, treasury_shares=0, shares_for_valuation=1000, reported_eps=10, reported_bps=100)])
        result = calculate_price_indicators(frame, manual_shares_outstanding=50)
        self.assertEqual(result["shares_outstanding"], 50)
        self.assertEqual(result["eps"], 2)
        self.assertEqual(result["bps"], 10)
        self.assertIsNone(calculate_price_indicators(frame, manual_shares_outstanding=-1)["eps"])

    def test_cache_reuses_same_filing_and_recovers_corruption(self):
        client = Mock()
        parsed = dict(facts_count=1, metrics={"issued_shares": 100}, details={})
        with tempfile.TemporaryDirectory() as temp, patch.object(service, "DATA_DIR", Path(temp)), patch.object(service, "extract_financial_data", return_value=parsed) as extract:
            self.assertEqual(service._parsed_document(client, "S1"), parsed)
            self.assertEqual(service._parsed_document(client, "S1"), parsed)
            self.assertEqual(extract.call_count, 1)
            self.assertEqual(client.download_document_zip.call_count, 1)
            cache_file = next((Path(temp) / "parsed").glob("*.json"))
            cache_file.write_text("broken", encoding="utf-8")
            service._parsed_document(client, "S1")
            service._parsed_document(client, "S2")
            self.assertEqual(extract.call_count, 3)

    def test_database_migrates_and_persists_new_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "test.db"
            row = dict(doc_id="S1", edinet_code="E00001", capital_stock=123, gross_profit=50,
                       reported_eps=94.93, reported_bps=338.45, accounting_standard="日本会計基準")
            save_financial_record(row, db)
            saved = load_financial_data("E00001", db).iloc[0]
            for key in ("capital_stock", "gross_profit", "reported_eps", "reported_bps", "accounting_standard"):
                self.assertEqual(saved[key], row[key])
            gc.collect()


if __name__ == "__main__":
    unittest.main()

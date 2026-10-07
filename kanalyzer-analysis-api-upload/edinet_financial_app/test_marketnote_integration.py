import math
import unittest
from decimal import Decimal, localcontext
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from analysis_api import create_app
from analysis_service import _default_dcf_model, _assess_dcf_suitability, build_analysis_payload
from dcf_analysis import calculate_historical_dcf_metrics
from financial_analysis import calculate_indicators
from xbrl_parser import ContextInfo, Fact, METRIC_SPECS, _extract_interest_bearing_debt, _extract_revenue, _extract_treasury_shares


def fact(name, value, context='CurrentYearInstant'):
    return Fact('test:'+name, name, value, context, 'JPY', 'test.xbrl')


class MarketnoteBackendTests(unittest.TestCase):
    def test_ifrs_revenue_aliases_keep_aggregate_separate_from_segments(self):
        contexts = {
            'CurrentYearDuration': ContextInfo('CurrentYearDuration', '2025-04-01', '2026-03-31'),
            'CurrentYearDuration_SegmentMember': ContextInfo('CurrentYearDuration_SegmentMember', '2025-04-01', '2026-03-31'),
            'PriorYearDuration': ContextInfo('PriorYearDuration', '2024-04-01', '2025-03-31'),
        }
        for tag in ('OperatingRevenuesIFRSKeyFinancialData', 'SalesRevenuesIFRS', 'OperatingRevenueFromExternalCustomersIFRS'):
            with self.subTest(tag=tag):
                value, _ = _extract_revenue(METRIC_SPECS['revenue'], [fact(tag, 100, 'PriorYearDuration'), fact(tag, 20, 'CurrentYearDuration_SegmentMember'), fact(tag, 120, 'CurrentYearDuration')], contexts)
                self.assertEqual(value, 120)

    def test_missing_debt_tags_are_unknown(self):
        value, detail = _extract_interest_bearing_debt([], {})
        self.assertIsNone(value)
        self.assertEqual(detail['method'], 'unavailable_no_debt_tags')

    def test_debt_totals_do_not_double_count_lease_detail(self):
        contexts = {'CurrentYearInstant': ContextInfo('CurrentYearInstant', instant='2026-03-31')}
        value, _ = _extract_interest_bearing_debt([fact('InterestBearingLiabilitiesCLIFRS', 10), fact('InterestBearingLiabilitiesNCLIFRS', 20), fact('LeaseLiabilitiesCLIFRS', 2)], contexts)
        self.assertEqual(value, 30)

    def test_one_missing_debt_total_is_not_a_complete_balance(self):
        contexts = {'CurrentYearInstant': ContextInfo('CurrentYearInstant', instant='2026-03-31')}
        value, _ = _extract_interest_bearing_debt([fact('InterestBearingLiabilitiesCLIFRS', 10)], contexts)
        self.assertIsNone(value)

    def test_an_explicit_zero_debt_balance_is_accepted(self):
        contexts = {'CurrentYearInstant': ContextInfo('CurrentYearInstant', instant='2026-03-31')}
        value, _ = _extract_interest_bearing_debt([fact('InterestBearingDebt', 0)], contexts)
        self.assertEqual(value, 0)

    def test_complete_dcf_has_forecast_terminal_value_and_sensitivity(self):
        model = _default_dcf_model(self.assumptions(), self.latest())
        self.assertNotIn('error', model)
        self.assertEqual(len(model['forecast']), 5)
        self.assertAlmostEqual(model['forecast'][0]['fcf'], 133)
        self.assertAlmostEqual(model['forecast'][1]['fcf'], 146.3)
        self.assertAlmostEqual(model['valuation']['equity_value'] - model['valuation']['enterprise_value'], 30)
        self.assertEqual(len(model['sensitivity']), 4)

    def test_missing_inputs_never_use_zero_or_thirty_percent_as_data(self):
        for source, keys in [('assumptions', self.assumptions()), ('latest', self.latest())]:
            for key in keys:
                with self.subTest(source=source, key=key):
                    assumptions, latest = self.assumptions(), self.latest()
                    (assumptions if source == 'assumptions' else latest)[key] = None
                    model = _default_dcf_model(assumptions, latest)
                    self.assertIn('error', model)
                    self.assertNotIn('valuation', model)

    def test_discounting_and_sensitivity_match_independent_decimal_calculation(self):
        model = _default_dcf_model(self.assumptions(), self.latest())
        with localcontext() as context:
            context.prec = 40
            flows = [Decimal('133') * Decimal('1.1') ** i for i in range(5)]
            def price(wacc, growth):
                discount = Decimal(1) + wacc
                pv = sum(flow / discount ** (i + 1) for i, flow in enumerate(flows))
                terminal = flows[-1] * (1 + growth) / (wacc - growth)
                return (pv + terminal / discount ** 5 + 80 - 50) / 100
            expected = price(Decimal('.08'), Decimal('.01'))
            self.assertAlmostEqual(model['valuation']['theoretical_share_price'], float(expected), places=10)
            for row in model['sensitivity']:
                growth = Decimal(str(row['永続成長率']))
                for column, actual in row.items():
                    if column.startswith('WACC '):
                        wacc = Decimal(column.split()[1].rstrip('%')) / 100
                        self.assertAlmostEqual(actual, float(price(wacc, growth)), places=10)

    def test_invalid_share_count_and_infinity_cannot_produce_a_price(self):
        for shares in (0, -1, math.inf):
            with self.subTest(shares=shares):
                self.assertIn('error', _default_dcf_model(self.assumptions(), dict(self.latest(), shares_for_valuation=shares)))

    def test_revenue_gap_is_not_forward_filled_into_growth(self):
        data = pd.DataFrame([dict(period_end='2024-03-31',revenue=100), dict(period_end='2025-03-31',revenue=None), dict(period_end='2026-03-31',revenue=120)])
        historical, _, _ = calculate_historical_dcf_metrics(data)
        self.assertTrue(historical['sales_growth_rate'].isna().all())

    def test_first_year_missing_change_in_working_capital_does_not_use_reported_fcf(self):
        data = pd.DataFrame([dict(period_end='2026-03-31',revenue=100,operating_income=20,pretax_income=20,income_taxes=5,depreciation=2,capex=3,accounts_receivable=10,inventories=5,accounts_payable=4,fcf=999)])
        historical, _, _ = calculate_historical_dcf_metrics(data)
        self.assertTrue(math.isnan(historical.iloc[0]['dcf_fcf']))
        self.assertEqual(historical.iloc[0]['dcf_nopat'],15)

    def test_financial_business_receivables_require_separate_scope(self):
        suitability = _assess_dcf_suitability(pd.Series(dict(financial_services_receivables_current=10)))
        self.assertEqual(suitability['status'],'requires_business_separation')
        response = build_analysis_payload(calculate_indicators(pd.DataFrame([dict(period_end='2026-03-31', financial_services_receivables_current=10)])))
        self.assertIn('error',response['dcf']['model'])
        self.assertNotIn('valuation',response['dcf']['model'])

    def test_no_detected_financial_business_does_not_mean_company_review_passed(self):
        self.assertEqual(_assess_dcf_suitability(pd.Series(dtype=object))['status'],'not_independently_reviewed')

    def test_reciprocal_holdings_grand_total_is_not_issuer_treasury_shares(self):
        contexts={'CurrentYearInstant':ContextInfo('CurrentYearInstant',instant='2026-03-31')}
        value, _ = _extract_treasury_shares(METRIC_SPECS['treasury_shares'],[fact('TotalNumberOfSharesHeldTreasurySharesEtc',100)],contexts)
        self.assertIsNone(value)

    def test_explicit_issuer_treasury_share_context_is_preserved(self):
        name='CurrentYearInstant_TreasurySharesMember'
        contexts={name:ContextInfo(name,instant='2026-03-31')}
        share=Fact('test:TotalNumberOfSharesHeldTreasurySharesEtc','TotalNumberOfSharesHeldTreasurySharesEtc',50,name,'shares','test.xbrl')
        value, _ = _extract_treasury_shares(METRIC_SPECS['treasury_shares'],[share],contexts)
        self.assertEqual(value,50)

    def test_voting_rights_treasury_etc_member_can_include_reciprocal_holdings(self):
        name='CurrentYearInstant_OrdinarySharesTreasurySharesSharesWithFullVotingRightsTreasurySharesEtcMember'
        contexts={name:ContextInfo(name,instant='2026-03-31')}
        share=Fact('test:NumberOfSharesIssuedSharesVotingRights','NumberOfSharesIssuedSharesVotingRights',100,name,'shares','test.xbrl')
        value, _ = _extract_treasury_shares(METRIC_SPECS['treasury_shares'],[share],contexts)
        self.assertIsNone(value)

    def test_production_preflight_allows_marketnote_and_rejects_unlisted_origin(self):
        with patch.dict('os.environ', {'KANALYZER_ALLOWED_ORIGINS': 'https://kanalyzer-edinet-finance.ktsht.chatgpt.site,https://market-note-jp.github.io'}):
            client = TestClient(create_app())
            headers = {'Origin':'https://market-note-jp.github.io','Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type'}
            response = client.options('/api/v1/annual-reports/search', headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['access-control-allow-origin'], headers['Origin'])
            self.assertEqual(client.options('/api/v1/annual-reports/search', headers=dict(headers, Origin='https://unlisted.example')).status_code, 400)

    @staticmethod
    def assumptions():
        return dict(sales_growth_rate=.1,operating_margin=.2,effective_tax_rate=.25,capex_rate=.04,depreciation_rate=.02,working_capital_ratio=.1)

    @staticmethod
    def latest():
        return dict(latest_revenue=1000,latest_cash=80,latest_debt=50,latest_working_capital=100,shares_for_valuation=100)

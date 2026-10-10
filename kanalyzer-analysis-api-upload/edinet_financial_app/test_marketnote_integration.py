import unittest
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from analysis_api import create_app
from analysis_service import build_analysis_payload
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





    def test_revenue_gap_is_not_forward_filled_into_growth(self):
        data = pd.DataFrame([dict(period_end='2024-03-31',revenue=100), dict(period_end='2025-03-31',revenue=None), dict(period_end='2026-03-31',revenue=120)])
        historical = calculate_indicators(data)
        self.assertTrue(historical['sales_growth_rate'].isna().all())


    def test_financial_business_receivables_require_separate_scope(self):
        response = build_analysis_payload(calculate_indicators(pd.DataFrame([dict(period_end='2026-03-31', financial_services_receivables_current=10)])))
        self.assertTrue(response['financialBusinessDetected'])
        self.assertNotIn('dcf', response)


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

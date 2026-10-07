from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup
from lxml import etree


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetricSpec:
    label: str
    period_type: str
    candidates: tuple[str, ...]
    components: tuple[str, ...] = ()
    allow_share_unit: bool = False
    allow_nonconsolidated: bool = False


@dataclass
class ContextInfo:
    context_id: str
    start_date: str | None = None
    end_date: str | None = None
    instant: str | None = None
    raw_text: str = ""

    @property
    def is_duration(self) -> bool:
        return bool(self.start_date and self.end_date)

    @property
    def is_instant(self) -> bool:
        return bool(self.instant)


@dataclass
class Fact:
    concept: str
    local_name: str
    value: float
    context_ref: str | None
    unit_ref: str | None
    source_file: str
    scale: int | None = None
    decimals: str | None = None


METRIC_SPECS: dict[str, MetricSpec] = {
    "financial_services_receivables_current": MetricSpec(
        "金融事業債権（流動）", "instant", ("ReceivablesRelatedToFinancialServicesCAIFRS",),
    ),
    "financial_services_receivables_noncurrent": MetricSpec(
        "金融事業債権（非流動）", "instant", ("ReceivablesRelatedToFinancialServicesNCAIFRS",),
    ),
    "revenue": MetricSpec(
        "売上高",
        "duration",
        (
            "RevenueIFRSSummaryOfBusinessResults",
            "OperatingRevenuesIFRSKeyFinancialData",
            "SalesRevenuesIFRS",
            "OperatingRevenueFromExternalCustomersIFRS",
            "RevenueFromExternalCustomers2IFRS",
            "RevenueFromExternalCustomersIFRS",
            "Revenue2IFRS",
            "NetSalesIFRS",
            "NetSales",
            "Revenue",
            "RevenueIFRS",
            "RevenueIFRSKeyFinancialData",
            "NetSalesIFRSKeyFinancialData",
            "SalesAndFinancialServicesRevenueIFRSKeyFinancialData",
            "SalesRevenue",
            "OperatingRevenue",
            "OperatingRevenue1",
            "OperatingRevenue1SummaryOfBusinessResults",
            "RevenueFromContractsWithCustomers",
            "RevenueFromContractsWithCustomersIFRS",
            "NetSalesSummaryOfBusinessResults",
        ),
    ),
    "operating_income": MetricSpec(
        "営業利益",
        "duration",
        (
            "OperatingProfitLossIFRS",
            "OperatingProfitIFRS",
            "OperatingIncomeIFRS",
            "OperatingProfitIFRSSummaryOfBusinessResults",
            "OperatingIncome",
            "OperatingProfit",
            "OperatingIncomeLoss",
            "OperatingProfitLoss",
            "OperatingIncomeSummaryOfBusinessResults",
        ),
    ),
    "ordinary_income": MetricSpec(
        "経常利益",
        "duration",
        (
            "OrdinaryIncome",
            "OrdinaryProfit",
            "OrdinaryIncomeLoss",
            "OrdinaryIncomeSummaryOfBusinessResults",
        ),
    ),
    "pretax_income": MetricSpec(
        "税引前当期純利益",
        "duration",
        (
            "ProfitLossBeforeTaxIFRSSummaryOfBusinessResults",
            "ProfitLossBeforeTaxIFRS",
            "ProfitLossBeforeTax",
            "ProfitBeforeTax",
            "IncomeBeforeIncomeTaxes",
            "IncomeBeforeIncomeTaxesAndMinorityInterests",
            "IncomeBeforeIncomeTaxesSummaryOfBusinessResults",
        ),
    ),
    "income_taxes": MetricSpec(
        "法人税等",
        "duration",
        (
            "IncomeTaxExpenseIFRS",
            "IncomeTaxExpenseOpeCFIFRS",
            "IncomeTaxes",
            "IncomeTaxExpense",
            "IncomeTaxesCurrent",
            "IncomeTaxesCurrentAndDeferred",
            "IncomeTaxesIncludingEnterpriseTax",
        ),
    ),
    "net_income": MetricSpec(
        "当期純利益",
        "duration",
        (
            "ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults",
            "ProfitLossAttributableToOwnersOfParentIFRS",
            "ProfitLossIFRSSummaryOfBusinessResults",
            "ProfitLossIFRS",
            "ProfitLossAttributableToOwnersOfParent",
            "ProfitLoss",
            "NetIncome",
            "NetIncomeLoss",
            "ProfitLossSummaryOfBusinessResults",
            "NetIncomeLossAttributableToOwnersOfParent",
        ),
    ),
    "total_assets": MetricSpec(
        "総資産",
        "instant",
        (
            "TotalAssetsIFRSSummaryOfBusinessResults",
            "AssetsIFRS",
            "Assets",
            "TotalAssets",
            "AssetsSummaryOfBusinessResults",
        ),
    ),
    "net_assets": MetricSpec(
        "純資産",
        "instant",
        (
            "TotalEquityIFRSSummaryOfBusinessResults",
            "EquityIFRS",
            "NetAssets",
            "NetAssetsSummaryOfBusinessResults",
            "Equity",
            "TotalEquity",
        ),
    ),
    "equity": MetricSpec(
        "自己資本",
        "instant",
        (
            "EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults",
            "EquityAttributableToOwnersOfParentIFRS",
            "EquityAttributableToOwnersOfParent",
            "TotalEquityAttributableToOwnersOfParent",
            "ShareholdersEquity",
            "OwnersEquity",
            "NetAssets",
        ),
    ),
    "operating_cf": MetricSpec(
        "営業CF",
        "duration",
        (
            "CashFlowsFromUsedInOperatingActivitiesIFRSSummaryOfBusinessResults",
            "NetCashProvidedByUsedInOperatingActivitiesIFRS",
            "NetCashProvidedByUsedInOperatingActivities",
            "CashFlowsFromUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesSummaryOfBusinessResults",
        ),
    ),
    "investing_cf": MetricSpec(
        "投資CF",
        "duration",
        (
            "CashFlowsFromUsedInInvestingActivitiesIFRSSummaryOfBusinessResults",
            "NetCashProvidedByUsedInInvestingActivitiesIFRS",
            "NetCashProvidedByUsedInInvestingActivities",
            "CashFlowsFromUsedInInvestingActivities",
            "NetCashProvidedByUsedInInvestingActivitiesSummaryOfBusinessResults",
        ),
    ),
    "financing_cf": MetricSpec(
        "財務CF",
        "duration",
        (
            "CashFlowsFromUsedInFinancingActivitiesIFRSSummaryOfBusinessResults",
            "NetCashProvidedByUsedInFinancingActivitiesIFRS",
            "NetCashProvidedByUsedInFinancingActivities",
            "CashFlowsFromUsedInFinancingActivities",
            "NetCashProvidedByUsedInFinancingActivitiesSummaryOfBusinessResults",
        ),
    ),
    "capex": MetricSpec(
        "設備投資",
        "duration",
        (
            "PurchaseOfPropertyPlantAndEquipmentInvCF",
            "PurchaseOfPropertyPlantAndEquipmentInvCFIFRS",
            "PurchaseOfIntangibleAssetsInvCF",
            "PurchaseOfIntangibleAssetsInvCFIFRS",
            "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCF",
            "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCFIFRS",
            "PaymentsForPurchaseOfPropertyPlantAndEquipmentAndIntangibleAssets",
            "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssets",
            "CapitalExpendituresOverviewOfCapitalExpendituresEtc",
            "CapitalExpenditures",
            "CapitalExpenditure",
        ),
    ),
    "depreciation": MetricSpec(
        "減価償却費",
        "duration",
        (
            "DepreciationExpenseOpeCFIFRS",
            "DepreciationAndAmortizationOpeCFIFRS",
            "DepreciationAndAmortisationOpeCFIFRS",
            "DepreciationAndAmortizationOpeCF",
            "DepreciationAndAmortization",
            "DepreciationAndAmortizationSGA",
            "DepreciationAndAmortizationCOS",
            "Depreciation",
            "DepreciationExpense",
        ),
    ),
    "rnd_expenses": MetricSpec(
        "研究開発費",
        "duration",
        (
            "ResearchAndDevelopmentExpenses",
            "ResearchAndDevelopmentExpense",
            "ResearchAndDevelopmentExpensesSGA",
            "ResearchAndDevelopmentExpensesIncludedInGeneralAndAdministrativeExpensesAndManufacturingCostsForCurrentPeriod",
        ),
    ),
    "inventories": MetricSpec(
        "棚卸資産",
        "instant",
        (
            "InventoriesCAIFRS",
            "Inventories",
            "MerchandiseAndFinishedGoods",
            "WorkInProcess",
            "RawMaterialsAndSupplies",
            "FinishedGoods",
            "Merchandise",
            "Supplies",
            "InventoriesSummaryOfBusinessResults",
        ),
    ),
    "accounts_receivable": MetricSpec(
        "売掛金",
        "instant",
        (
            "TradeReceivablesCAIFRS",
            "TradeAndOtherReceivablesCAIFRS",
            "NotesAndAccountsReceivableTrade",
            "AccountsReceivableTrade",
            "TradeReceivables",
            "NotesAndAccountsReceivableTradeAndContractAssets",
            "OperatingAccountsReceivable",
        ),
    ),
    "accounts_payable": MetricSpec(
        "買掛金",
        "instant",
        (
            "TradePayablesCLIFRS",
            "TradeAndOtherPayablesCLIFRS",
            "NotesAndAccountsPayableTrade",
            "AccountsPayableTrade",
            "TradePayables",
            "OperatingAccountsPayable",
        ),
    ),
    "interest_bearing_debt": MetricSpec(
        "有利子負債",
        "instant",
        (
            "InterestBearingDebt",
            "InterestBearingLiabilities",
            "InterestBearingDebtSummaryOfBusinessResults",
        ),
        components=(
            "BondsAndBorrowingsCLIFRS",
            "BondsAndBorrowingsNCLIFRS",
            "BorrowingsCLIFRS",
            "BorrowingsNCLIFRS",
            "LeaseLiabilitiesCLIFRS",
            "LeaseLiabilitiesNCLIFRS",
            "LeaseObligationsCL",
            "LeaseObligationsNCL",
            "CurrentBorrowings",
            "NonCurrentBorrowings",
            "CurrentLeaseLiabilities",
            "NonCurrentLeaseLiabilities",
            "ShortTermBorrowings",
            "ShortTermLoansPayable",
            "CurrentPortionOfLongTermBorrowings",
            "CurrentPortionOfBondsPayable",
            "CurrentPortionOfBonds",
            "CommercialPapersLiabilities",
            "BondsPayable",
            "LongTermBorrowings",
            "LongTermLoansPayable",
            "LeaseObligations",
            "CurrentPortionOfLeaseObligations",
        ),
    ),
    "cash_and_equivalents": MetricSpec(
        "現金及び現金同等物",
        "any",
        (
            "CashAndCashEquivalentsIFRSSummaryOfBusinessResults",
            "CashAndCashEquivalentsIFRS",
            "CashAndCashEquivalents",
            "CashAndCashEquivalentsAtEndOfPeriod",
            "CashAndDeposits",
            "CashAndCashEquivalentsSummaryOfBusinessResults",
        ),
    ),
    "issued_shares": MetricSpec(
        "発行済株式総数",
        "instant",
        (
            "NumberOfIssuedSharesAsOfFiscalYearEndIssuedSharesTotalNumberOfSharesEtc",
            "TotalNumberOfIssuedSharesSummaryOfBusinessResults",
            "TotalNumberOfIssuedShares",
            "NumberOfIssuedSharesAsOfFiscalYearEnd",
            "NumberOfIssuedSharesAsOfFilingDateIssuedSharesTotalNumberOfSharesEtc",
            "NumberOfSharesIssuedSharesVotingRights",
        ),
        allow_share_unit=True,
        allow_nonconsolidated=True,
    ),
    "treasury_shares": MetricSpec(
        "自己株式数",
        "instant",
        (
            "TotalNumberOfSharesHeldTreasurySharesEtc",
            "NumberOfSharesHeldInOwnNameTreasurySharesEtc",
            "NumberOfTreasuryShares",
            "TotalNumberOfTreasuryShares",
            "NumberOfSharesHeldTreasuryShares",
            "NumberOfSharesOfTreasuryShares",
        ),
        allow_share_unit=True,
        allow_nonconsolidated=True,
    ),
}

METRIC_LABELS = {key: spec.label for key, spec in METRIC_SPECS.items()}

CAPEX_PPE_PURCHASE_TAGS = (
    "PurchaseOfPropertyPlantAndEquipmentInvCF",
    "PurchaseOfPropertyPlantAndEquipmentInvCFIFRS",
)
CAPEX_INTANGIBLE_PURCHASE_TAGS = (
    "PurchaseOfIntangibleAssetsInvCF",
    "PurchaseOfIntangibleAssetsInvCFIFRS",
)
CAPEX_COMBINED_PURCHASE_TAGS = (
    "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCF",
    "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCFIFRS",
    "PaymentsForPurchaseOfPropertyPlantAndEquipmentAndIntangibleAssets",
    "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssets",
)
CAPEX_OVERVIEW_TAGS = (
    "CapitalExpendituresOverviewOfCapitalExpendituresEtc",
    "CapitalExpenditures",
    "CapitalExpenditure",
)
CAPEX_PPE_ASSET_TAGS = (
    "PropertyPlantAndEquipmentIFRS",
    "PropertyPlantAndEquipment",
    "PropertyPlantAndEquipmentNet",
    "PropertyPlantAndEquipmentSummaryOfBusinessResults",
)
CAPEX_INTANGIBLE_ASSET_TAGS = (
    "IntangibleAssetsIFRS",
    "IntangibleAssets",
    "IntangibleAssetsSummaryOfBusinessResults",
)
CAPEX_DEPRECIATION_TAGS = (
    "DepreciationExpenseOpeCFIFRS",
    "DepreciationAndAmortizationOpeCFIFRS",
    "DepreciationAndAmortisationOpeCFIFRS",
    "DepreciationAndAmortizationOpeCF",
    "DepreciationAndAmortization",
    "DepreciationAndAmortizationSGA",
    "DepreciationAndAmortizationCOS",
    "Depreciation",
    "DepreciationExpense",
)

INTEREST_BEARING_DEBT_TOTAL_TAGS = (
    "InterestBearingDebt",
    "InterestBearingLiabilities",
    "InterestBearingDebtSummaryOfBusinessResults",
)
INTEREST_BEARING_DEBT_COMBINED_BORROWINGS_TAGS = (
    "BondsAndBorrowingsCLIFRS",
    "BondsAndBorrowingsNCLIFRS",
)
INTEREST_BEARING_DEBT_COMBINED_LEASE_TAGS = (
    "LeaseLiabilitiesCLIFRS",
    "LeaseLiabilitiesNCLIFRS",
    "CurrentLeaseLiabilities",
    "NonCurrentLeaseLiabilities",
    "LeaseObligationsCL",
    "LeaseObligationsNCL",
)
INTEREST_BEARING_DEBT_GROUPS = (
    (
        "current_borrowings",
        (("BorrowingsCLIFRS", "CurrentBorrowings"),),
        (("ShortTermBorrowings", "ShortTermLoansPayable"), ("CurrentPortionOfLongTermBorrowings",)),
    ),
    (
        "noncurrent_borrowings",
        (("BorrowingsNCLIFRS", "NonCurrentBorrowings"),),
        (("LongTermBorrowings", "LongTermLoansPayable"),),
    ),
    (
        "current_bonds",
        (),
        (("CurrentPortionOfBondsPayable", "CurrentPortionOfBonds"),),
    ),
    (
        "noncurrent_bonds",
        (),
        (("BondsPayable",),),
    ),
    (
        "current_leases",
        (("LeaseLiabilitiesCLIFRS", "CurrentLeaseLiabilities", "LeaseObligationsCL"),),
        (("CurrentPortionOfLeaseObligations",),),
    ),
    (
        "noncurrent_leases",
        (("LeaseLiabilitiesNCLIFRS", "NonCurrentLeaseLiabilities", "LeaseObligationsNCL"),),
        (("LeaseObligations",),),
    ),
)
INTEREST_BEARING_DEBT_FACT_TAGS = tuple(
    dict.fromkeys(
        [
            *INTEREST_BEARING_DEBT_TOTAL_TAGS,
            *INTEREST_BEARING_DEBT_COMBINED_BORROWINGS_TAGS,
            *INTEREST_BEARING_DEBT_COMBINED_LEASE_TAGS,
            "BorrowingsCLIFRS",
            "BorrowingsNCLIFRS",
            "CurrentBorrowings",
            "NonCurrentBorrowings",
            "ShortTermBorrowings",
            "ShortTermLoansPayable",
            "CurrentPortionOfLongTermBorrowings",
            "LongTermBorrowings",
            "LongTermLoansPayable",
            "BondsPayable",
            "CurrentPortionOfBondsPayable",
            "CurrentPortionOfBonds",
            "CommercialPapersLiabilities",
            "CurrentPortionOfLeaseObligations",
            "LeaseObligations",
        ]
    )
)


def extract_financial_data(extracted_dir: str | Path) -> dict:
    extracted_dir = Path(extracted_dir)
    facts: list[Fact] = []
    contexts: dict[str, ContextInfo] = {}

    for file_path in _candidate_files(extracted_dir):
        try:
            file_facts, file_contexts = _parse_file(file_path)
        except Exception:
            continue
        facts.extend(file_facts)
        contexts.update(file_contexts)

    metrics: dict[str, float | None] = {}
    details: dict[str, dict | None] = {}
    prefer_consolidated = _has_consolidated_facts(facts, contexts)

    for key, spec in METRIC_SPECS.items():
        if key == "revenue":
            value, detail = _extract_revenue(spec, facts, contexts, prefer_consolidated=prefer_consolidated)
        elif key == "treasury_shares":
            value, detail = _extract_treasury_shares(spec, facts, contexts, prefer_consolidated=prefer_consolidated)
        elif key == "capex":
            value, detail = _extract_capex(facts, contexts, prefer_consolidated=prefer_consolidated)
        elif key == "interest_bearing_debt":
            value, detail = _extract_interest_bearing_debt(facts, contexts, prefer_consolidated=prefer_consolidated)
        else:
            value, detail = _extract_metric(key, spec, facts, contexts, prefer_consolidated=prefer_consolidated)
        metrics[key] = value
        details[key] = detail

    _add_valuation_share_count(metrics, details)

    return {
        "metrics": metrics,
        "details": details,
        "facts_count": len(facts),
        "contexts_count": len(contexts),
        "prefer_consolidated": prefer_consolidated,
        "source_dir": str(extracted_dir),
    }


def _candidate_files(root: Path) -> list[Path]:
    suffixes = {".xbrl", ".xml", ".htm", ".html", ".xhtml"}
    skipped_suffixes = {".xsd", ".css", ".js"}
    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in skipped_suffixes or suffix not in suffixes:
            continue
        name = path.name.lower()
        if "manifest" in name or "taxonomy" in name:
            continue
        files.append(path)

    def score(path: Path) -> int:
        text = str(path).replace("\\", "/").lower()
        value = 0
        if "/publicdoc/" in text:
            value += 30
        if "/auditdoc/" in text:
            value -= 20
        if "jpcrp" in text:
            value += 15
        if path.suffix.lower() == ".xbrl":
            value += 20
        if path.suffix.lower() in {".htm", ".html", ".xhtml"}:
            value += 10
        return value

    return sorted(files, key=score, reverse=True)


def _parse_file(path: Path) -> tuple[list[Fact], dict[str, ContextInfo]]:
    suffix = path.suffix.lower()
    if suffix in {".htm", ".html", ".xhtml"}:
        return _parse_inline_xbrl(path)
    return _parse_xml_xbrl(path)


def _parse_inline_xbrl(path: Path) -> tuple[list[Fact], dict[str, ContextInfo]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(text, "lxml")
    contexts = _parse_contexts_from_soup(soup)
    facts: list[Fact] = []

    for tag in soup.find_all():
        tag_name = (tag.name or "").lower()
        if not tag_name.endswith("nonfraction"):
            continue

        concept = _attr(tag, "name")
        context_ref = _attr(tag, "contextref")
        if not concept or not context_ref:
            continue
        raw_text = tag.get_text("", strip=True)
        scale = _safe_int(_attr(tag, "scale"))
        value = _parse_number(raw_text, scale=scale, sign=_attr(tag, "sign"))
        if value is None:
            continue

        facts.append(
            Fact(
                concept=concept,
                local_name=_local_name(concept),
                value=value,
                context_ref=context_ref,
                unit_ref=_attr(tag, "unitref"),
                source_file=str(path),
                scale=scale,
                decimals=_attr(tag, "decimals"),
            )
        )

    return facts, contexts


def _parse_xml_xbrl(path: Path) -> tuple[list[Fact], dict[str, ContextInfo]]:
    parser = etree.XMLParser(recover=True, huge_tree=True)
    tree = etree.parse(str(path), parser)
    contexts = _parse_contexts_from_tree(tree)
    facts: list[Fact] = []

    for element in tree.iter():
        context_ref = element.attrib.get("contextRef") or element.attrib.get("contextref")
        if not context_ref:
            continue
        text = "".join(element.itertext()).strip()
        value = _parse_number(text)
        if value is None:
            continue
        concept = _element_name(element)
        facts.append(
            Fact(
                concept=concept,
                local_name=_local_name(concept),
                value=value,
                context_ref=context_ref,
                unit_ref=element.attrib.get("unitRef") or element.attrib.get("unitref"),
                source_file=str(path),
                decimals=element.attrib.get("decimals"),
            )
        )

    return facts, contexts


def _parse_contexts_from_soup(soup: BeautifulSoup) -> dict[str, ContextInfo]:
    contexts: dict[str, ContextInfo] = {}
    for tag in soup.find_all(lambda item: item.name and item.name.lower().endswith("context")):
        context_id = _attr(tag, "id")
        if not context_id:
            continue
        contexts[context_id] = ContextInfo(
            context_id=context_id,
            start_date=_child_text(tag, "startdate"),
            end_date=_child_text(tag, "enddate"),
            instant=_child_text(tag, "instant"),
            raw_text=tag.get_text(" ", strip=True),
        )
    return contexts


def _parse_contexts_from_tree(tree: etree._ElementTree) -> dict[str, ContextInfo]:
    contexts: dict[str, ContextInfo] = {}
    for element in tree.iter():
        if _local_name(_element_name(element)).lower() != "context":
            continue
        context_id = element.attrib.get("id")
        if not context_id:
            continue
        contexts[context_id] = ContextInfo(
            context_id=context_id,
            start_date=_first_child_text(element, "startDate"),
            end_date=_first_child_text(element, "endDate"),
            instant=_first_child_text(element, "instant"),
            raw_text=" ".join(piece.strip() for piece in element.itertext() if piece.strip()),
        )
    return contexts


def _extract_metric(
    key: str,
    spec: MetricSpec,
    facts: list[Fact],
    contexts: dict[str, ContextInfo],
    prefer_consolidated: bool = True,
) -> tuple[float | None, dict | None]:
    direct_value, direct_detail = _best_value_for_names(
        facts,
        contexts,
        names=tuple(_normalize_name(item) for item in spec.candidates),
        period_type=spec.period_type,
        exclude_nonconsolidated=prefer_consolidated and not spec.allow_nonconsolidated,
        allow_share_unit=spec.allow_share_unit,
    )
    if direct_value is not None:
        return direct_value, direct_detail

    component_names = tuple(_normalize_name(item) for item in spec.components)
    if not component_names:
        return None, None

    component_details = []
    total = 0.0
    for component_name in component_names:
        value, detail = _best_value_for_names(
            facts,
            contexts,
            names=(component_name,),
            period_type=spec.period_type,
            exclude_nonconsolidated=prefer_consolidated and not spec.allow_nonconsolidated,
            allow_share_unit=spec.allow_share_unit,
        )

        if value is None:
            continue
        total += value
        component_details.append(detail)

    if not component_details:
        return None, None

    return total, {
        "value": total,
        "method": "sum_components",
        "components": component_details,
    }


def _extract_revenue(
    spec: MetricSpec,
    facts: list[Fact],
    contexts: dict[str, ContextInfo],
    prefer_consolidated: bool = True,
) -> tuple[float | None, dict]:
    names = tuple(_normalize_name(item) for item in spec.candidates)
    value, selected = _best_value_for_names(
        facts,
        contexts,
        names=names,
        period_type=spec.period_type,
        exclude_nonconsolidated=prefer_consolidated and not spec.allow_nonconsolidated,
        allow_share_unit=spec.allow_share_unit,
        context_exclude=("member",),
    )
    candidates = _revenue_candidate_debug(
        facts,
        contexts,
        names=names,
        selected=selected,
        exclude_nonconsolidated=prefer_consolidated and not spec.allow_nonconsolidated,
    )
    debug = {
        "candidate_tags": sorted({item["local_name"] for item in candidates}),
        "candidates": candidates,
        "selected": selected,
    }

    if selected is None:
        logger.warning(
            "売上高を取得できませんでした。候補タグ診断: %s",
            json.dumps(debug, ensure_ascii=False, default=str),
        )
        return None, {
            "value": None,
            "method": "not_found",
            "debug": debug,
        }

    logger.debug("売上高候補タグ診断: %s", json.dumps(debug, ensure_ascii=False, default=str))
    return value, {
        **selected,
        "method": "direct_tag",
        "debug": debug,
    }


def _revenue_candidate_debug(
    facts: list[Fact],
    contexts: dict[str, ContextInfo],
    names: tuple[str, ...],
    selected: dict | None,
    exclude_nonconsolidated: bool,
) -> list[dict]:
    name_rank = {name: len(names) - index for index, name in enumerate(names)}
    grouped: dict[tuple, list[Fact]] = {}
    for fact in facts:
        if not _looks_like_revenue_fact(fact.local_name):
            continue
        key = (
            _normalize_name(fact.local_name),
            fact.context_ref,
            fact.unit_ref,
            fact.value,
        )
        grouped.setdefault(key, []).append(fact)

    rows = []
    for group in grouped.values():
        fact = group[0]
        normalized_name = _normalize_name(fact.local_name)
        context = contexts.get(fact.context_ref or "")
        context_text = f"{fact.context_ref or ''} {context.raw_text if context else ''}".lower()
        score = _fact_score(fact, context, "duration") + name_rank.get(normalized_name, 0)
        is_selected = _matches_selected_revenue_fact(fact, selected)
        reasons: list[str] = []

        if not is_selected:
            if normalized_name not in name_rank:
                reasons.append("売上高の候補タグ定義外")
            if _is_unwanted_unit(fact.unit_ref):
                reasons.append("金額単位ではない")
            if exclude_nonconsolidated and _is_nonconsolidated_context(fact, context):
                reasons.append("個別（非連結）context")
            if context is None:
                reasons.append("contextRefに対応するcontextがない")
            elif not context.is_duration:
                reasons.append("期間（duration）contextではない")
            if "member" in context_text:
                reasons.append("セグメント・メンバー別context")
            if any(term in (fact.context_ref or "").lower() for term in ("prior", "previous")):
                reasons.append("過年度context")
            if not reasons:
                reasons.append("採用候補より優先順位が低い、または重複")

        rows.append(
            {
                "local_name": fact.local_name,
                "concepts": sorted({item.concept for item in group}),
                "context_ref": fact.context_ref,
                "unit_ref": fact.unit_ref,
                "value": fact.value,
                "context_period": _context_period(context),
                "rank_score": score,
                "status": "adopted" if is_selected else "rejected",
                "reasons": [] if is_selected else reasons,
                "source_files": sorted({item.source_file for item in group}),
                "duplicate_count": len(group),
            }
        )

    return sorted(
        rows,
        key=lambda item: (item["status"] == "adopted", item["rank_score"]),
        reverse=True,
    )


def _looks_like_revenue_fact(local_name: str) -> bool:
    normalized = _normalize_name(local_name)
    prefixes = (
        "revenue",
        "revenues",
        "netrevenue",
        "netsales",
        "salesrevenue",
        "operatingrevenue",
        "intersegmentrevenue",
        "intersegmentsales",
        "salestoexternalcustomers",
        "salesandfinancialservicesrevenue",
    )
    return normalized.startswith(prefixes)


def _matches_selected_revenue_fact(fact: Fact, selected: dict | None) -> bool:
    if not selected:
        return False
    return (
        _normalize_name(fact.local_name) == _normalize_name(str(selected.get("local_name") or ""))
        and fact.context_ref == selected.get("context_ref")
        and fact.unit_ref == selected.get("unit_ref")
        and fact.value == selected.get("value")
    )


def _extract_interest_bearing_debt(
    facts: list[Fact],
    contexts: dict[str, ContextInfo],
    prefer_consolidated: bool = True,
) -> tuple[float | None, dict | None]:
    direct_value, direct_detail = _best_debt_current_value(
        facts,
        contexts,
        INTEREST_BEARING_DEBT_TOTAL_TAGS,
        prefer_consolidated=prefer_consolidated,
    )
    if direct_value is not None:
        return abs(direct_value), _debt_detail(
            abs(direct_value),
            "direct_total",
            direct_detail=direct_detail,
            raw_value=direct_value,
        )

    # These issuer totals already include interest-bearing obligations.
    # Do not add borrowing/lease detail a second time.
    liability_parts = []
    for name in ("InterestBearingLiabilitiesCLIFRS", "InterestBearingLiabilitiesNCLIFRS"):
        value, detail = _best_debt_current_value(facts, contexts, (name,), prefer_consolidated=prefer_consolidated)
        if value is not None:
            liability_parts.append(_debt_component_detail(name, value, detail))
    if liability_parts:
        if len(liability_parts) != 2:
            return None, {"value": None, "method": "incomplete_current_noncurrent_total", "note": "Current or non-current interest-bearing liability total is missing."}
        total = sum(part["value"] for part in liability_parts)
        return total, _debt_detail(total, "current_noncurrent_interest_bearing_totals", component_details=liability_parts)

    combined_components = []
    combined_total = 0.0
    for label, names in (
        ("current_bonds_and_borrowings", ("BondsAndBorrowingsCLIFRS",)),
        ("noncurrent_bonds_and_borrowings", ("BondsAndBorrowingsNCLIFRS",)),
    ):
        value, detail = _best_debt_current_value(facts, contexts, names, prefer_consolidated=prefer_consolidated)
        if value is None:
            continue
        combined_total += abs(value)
        combined_components.append(_debt_component_detail(label, value, detail))

    if combined_components:
        for label, names in (
            ("current_leases", ("LeaseLiabilitiesCLIFRS", "CurrentLeaseLiabilities", "LeaseObligationsCL")),
            ("noncurrent_leases", ("LeaseLiabilitiesNCLIFRS", "NonCurrentLeaseLiabilities", "LeaseObligationsNCL")),
        ):
            value, detail = _best_debt_current_value(facts, contexts, names, prefer_consolidated=prefer_consolidated)
            if value is None:
                continue
            combined_total += abs(value)
            combined_components.append(_debt_component_detail(label, value, detail))
        return combined_total, _debt_detail(
            combined_total,
            "bonds_and_borrowings_plus_leases",
            component_details=combined_components,
        )

    component_details = []
    total = 0.0
    for group_name, preferred_groups, fallback_groups in INTEREST_BEARING_DEBT_GROUPS:
        value, detail = _debt_group_value(
            facts,
            contexts,
            preferred_groups=preferred_groups,
            fallback_groups=fallback_groups,
            prefer_consolidated=prefer_consolidated,
        )
        if value is None:
            continue
        total += abs(value)
        component_details.append(_debt_component_detail(group_name, value, detail))

    if component_details:
        return total, _debt_detail(total, "sum_components", component_details=component_details)

    if not _has_current_debt_balance_sheet_fact(facts, contexts, prefer_consolidated=prefer_consolidated):
        return None, {
            "value": None,
            "method": "unavailable_no_debt_tags",
            "source_tags": [],
            "note": "No recognized current interest-bearing debt tags were found; the balance is unknown, not zero.",
        }

    return None, None


def _best_debt_current_value(
    facts: list[Fact],
    contexts: dict[str, ContextInfo],
    names: tuple[str, ...],
    prefer_consolidated: bool,
) -> tuple[float | None, dict | None]:
    return _best_current_instant_value(facts, contexts, names, prefer_consolidated=prefer_consolidated)


def _debt_group_value(
    facts: list[Fact],
    contexts: dict[str, ContextInfo],
    preferred_groups: tuple[tuple[str, ...], ...],
    fallback_groups: tuple[tuple[str, ...], ...],
    prefer_consolidated: bool,
) -> tuple[float | None, dict | None]:
    for names in preferred_groups:
        value, detail = _best_debt_current_value(facts, contexts, names, prefer_consolidated=prefer_consolidated)
        if value is not None:
            return value, detail

    total = 0.0
    components = []
    for names in fallback_groups:
        value, detail = _best_debt_current_value(facts, contexts, names, prefer_consolidated=prefer_consolidated)
        if value is None:
            continue
        total += value
        components.append(detail)

    if not components:
        return None, None
    if len(components) == 1:
        return total, components[0]
    return total, {
        "value": total,
        "method": "sum_subcomponents",
        "components": components,
    }


def _debt_component_detail(label: str, raw_value: float, detail: dict | None) -> dict:
    component = {
        "component": label,
        "raw_value": raw_value,
        "value": abs(raw_value),
    }
    if detail:
        component.update(detail)
        component["raw_value"] = raw_value
        component["value"] = abs(raw_value)
    return component


def _debt_detail(
    value: float,
    method: str,
    raw_value: float | None = None,
    direct_detail: dict | None = None,
    component_details: list[dict] | None = None,
) -> dict:
    detail = dict(direct_detail or {})
    if raw_value is not None:
        detail["raw_value"] = raw_value
    detail["value"] = value
    detail["method"] = method
    if component_details is not None:
        detail["components"] = component_details

    source_tags = []
    if direct_detail and direct_detail.get("local_name"):
        source_tags.append(direct_detail["local_name"])
    for component in component_details or []:
        tag = component.get("local_name")
        if tag and tag not in source_tags:
            source_tags.append(tag)
        for subcomponent in component.get("components", []) or []:
            subtag = subcomponent.get("local_name")
            if subtag and subtag not in source_tags:
                source_tags.append(subtag)
    detail["source_tags"] = source_tags
    return detail


def _has_current_debt_balance_sheet_fact(
    facts: list[Fact],
    contexts: dict[str, ContextInfo],
    prefer_consolidated: bool,
) -> bool:
    names = {_normalize_name(item) for item in INTEREST_BEARING_DEBT_FACT_TAGS}
    for fact in facts:
        if _normalize_name(fact.local_name) not in names:
            continue
        if _is_unwanted_unit(fact.unit_ref, allow_share_unit=False):
            continue
        context = contexts.get(fact.context_ref or "")
        if prefer_consolidated and _is_nonconsolidated_context(fact, context):
            continue
        if not _context_matches(fact, context, context_include=(), context_exclude=("prior", "previous")):
            continue
        if context and context.is_instant:
            return True
    return False


def _extract_treasury_shares(
    spec: MetricSpec,
    facts: list[Fact],
    contexts: dict[str, ContextInfo],
    prefer_consolidated: bool = True,
) -> tuple[float | None, dict | None]:
    value, detail = _best_value_for_names(
        facts,
        contexts,
        names=("totalnumberofsharesheldtreasurysharesetc",),
        period_type=spec.period_type,
        exclude_nonconsolidated=prefer_consolidated and not spec.allow_nonconsolidated,
        allow_share_unit=True,
        context_include=("treasuryshares",),
        context_exclude=("reciprocal", "othermember", "shareslessthanoneunit"),
    )
    if detail is not None:
        detail["method"] = "context_filtered_treasury_shares"
        return value, detail

    # The voting-rights table's totals, including its TreasurySharesEtcMember,
    # can also include reciprocal holdings and omit less-than-one-unit shares.
    # They are not a reliable exact issuer treasury-stock count.
    safe_spec = MetricSpec(spec.label, spec.period_type, (
        "NumberOfTreasuryShares", "TotalNumberOfTreasuryShares",
        "NumberOfSharesOfTreasuryShares",
    ), allow_share_unit=True, allow_nonconsolidated=True)
    value, detail = _extract_metric("treasury_shares", safe_spec, facts, contexts, prefer_consolidated=prefer_consolidated)
    if value is None:
        return None, {"value": None, "method": "unavailable_ambiguous_voting_rights_total", "note": "Treasury shares could not be distinguished from reciprocal holdings; manual confirmation is required."}
    return value, detail


def _extract_capex(
    facts: list[Fact],
    contexts: dict[str, ContextInfo],
    prefer_consolidated: bool = True,
) -> tuple[float | None, dict | None]:
    ppe_value, ppe_detail = _best_capex_duration_value(
        facts,
        contexts,
        CAPEX_PPE_PURCHASE_TAGS,
        prefer_consolidated=prefer_consolidated,
    )
    intangible_value, intangible_detail = _best_capex_duration_value(
        facts,
        contexts,
        CAPEX_INTANGIBLE_PURCHASE_TAGS,
        prefer_consolidated=prefer_consolidated,
    )
    component_details = []
    total = 0.0
    if ppe_value is not None:
        total += abs(ppe_value)
        component_details.append(_capex_component_detail("property_plant_and_equipment", ppe_value, ppe_detail))
    if intangible_value is not None:
        total += abs(intangible_value)
        component_details.append(_capex_component_detail("intangible_assets", intangible_value, intangible_detail))
    partial_component_details = component_details
    partial_total = total
    if ppe_value is not None and intangible_value is not None:
        return total, _capex_detail(
            total,
            "direct_ppe_plus_intangible",
            component_details=component_details,
        )

    combined_value, combined_detail = _best_capex_duration_value(
        facts,
        contexts,
        CAPEX_COMBINED_PURCHASE_TAGS,
        prefer_consolidated=prefer_consolidated,
    )
    if combined_value is not None:
        value = abs(combined_value)
        return value, _capex_detail(
            value,
            "direct_combined",
            raw_value=combined_value,
            direct_detail=combined_detail,
        )

    overview_value, overview_detail = _best_capex_duration_value(
        facts,
        contexts,
        CAPEX_OVERVIEW_TAGS,
        prefer_consolidated=prefer_consolidated,
    )
    if overview_value is not None:
        value = abs(overview_value)
        return value, _capex_detail(
            value,
            "capital_expenditure_overview",
            raw_value=overview_value,
            direct_detail=overview_detail,
        )

    if partial_component_details:
        return partial_total, _capex_detail(
            partial_total,
            "direct_ppe_plus_intangible",
            component_details=partial_component_details,
        )

    estimate = _estimate_capex_from_assets(facts, contexts, prefer_consolidated=prefer_consolidated)
    if estimate is not None:
        value = abs(estimate["raw_value"])
        return value, _capex_detail(
            value,
            "estimated_from_assets",
            raw_value=estimate["raw_value"],
            component_details=estimate["components"],
        )

    return None, None


def _best_capex_duration_value(
    facts: list[Fact],
    contexts: dict[str, ContextInfo],
    names: tuple[str, ...],
    prefer_consolidated: bool,
) -> tuple[float | None, dict | None]:
    return _best_value_for_names(
        facts,
        contexts,
        names=tuple(_normalize_name(item) for item in names),
        period_type="duration",
        exclude_nonconsolidated=prefer_consolidated,
    )


def _estimate_capex_from_assets(
    facts: list[Fact],
    contexts: dict[str, ContextInfo],
    prefer_consolidated: bool,
) -> dict | None:
    ppe_current, ppe_current_detail = _best_current_instant_value(
        facts,
        contexts,
        CAPEX_PPE_ASSET_TAGS,
        prefer_consolidated=prefer_consolidated,
    )
    ppe_prior, ppe_prior_detail = _best_prior_instant_value(
        facts,
        contexts,
        CAPEX_PPE_ASSET_TAGS,
        prefer_consolidated=prefer_consolidated,
    )
    intangible_current, intangible_current_detail = _best_current_instant_value(
        facts,
        contexts,
        CAPEX_INTANGIBLE_ASSET_TAGS,
        prefer_consolidated=prefer_consolidated,
    )
    intangible_prior, intangible_prior_detail = _best_prior_instant_value(
        facts,
        contexts,
        CAPEX_INTANGIBLE_ASSET_TAGS,
        prefer_consolidated=prefer_consolidated,
    )
    depreciation, depreciation_detail = _best_capex_duration_value(
        facts,
        contexts,
        CAPEX_DEPRECIATION_TAGS,
        prefer_consolidated=prefer_consolidated,
    )
    values = (ppe_current, ppe_prior, intangible_current, intangible_prior, depreciation)
    if any(value is None for value in values):
        return None

    raw_value = ppe_current + intangible_current - ppe_prior - intangible_prior + depreciation
    return {
        "raw_value": raw_value,
        "components": [
            _capex_component_detail("current_property_plant_and_equipment", ppe_current, ppe_current_detail),
            _capex_component_detail("prior_property_plant_and_equipment", ppe_prior, ppe_prior_detail),
            _capex_component_detail("current_intangible_assets", intangible_current, intangible_current_detail),
            _capex_component_detail("prior_intangible_assets", intangible_prior, intangible_prior_detail),
            _capex_component_detail("depreciation", depreciation, depreciation_detail),
        ],
    }


def _best_current_instant_value(
    facts: list[Fact],
    contexts: dict[str, ContextInfo],
    names: tuple[str, ...],
    prefer_consolidated: bool,
) -> tuple[float | None, dict | None]:
    normalized_names = tuple(_normalize_name(item) for item in names)
    value, detail = _best_value_for_names(
        facts,
        contexts,
        names=normalized_names,
        period_type="instant",
        exclude_nonconsolidated=prefer_consolidated,
        context_include=("current",),
        context_exclude=("prior", "previous"),
    )
    if value is not None:
        return value, detail
    return _best_value_for_names(
        facts,
        contexts,
        names=normalized_names,
        period_type="instant",
        exclude_nonconsolidated=prefer_consolidated,
        context_exclude=("prior", "previous"),
    )


def _best_prior_instant_value(
    facts: list[Fact],
    contexts: dict[str, ContextInfo],
    names: tuple[str, ...],
    prefer_consolidated: bool,
) -> tuple[float | None, dict | None]:
    normalized_names = tuple(_normalize_name(item) for item in names)
    for include_terms in (("prior1year",), ("prioryear",), ("prior",), ("previous",)):
        value, detail = _best_value_for_names(
            facts,
            contexts,
            names=normalized_names,
            period_type="instant",
            exclude_nonconsolidated=prefer_consolidated,
            context_include=include_terms,
            context_exclude=("current",),
        )
        if value is not None:
            return value, detail
    return None, None


def _capex_component_detail(label: str, raw_value: float, detail: dict | None) -> dict:
    component = {
        "component": label,
        "raw_value": raw_value,
        "value": abs(raw_value),
    }
    if detail:
        component.update(detail)
        component["raw_value"] = raw_value
        component["value"] = abs(raw_value)
    return component


def _capex_detail(
    value: float,
    method: str,
    raw_value: float | None = None,
    direct_detail: dict | None = None,
    component_details: list[dict] | None = None,
) -> dict:
    detail = dict(direct_detail or {})
    if raw_value is not None:
        detail["raw_value"] = raw_value
    detail["value"] = value
    detail["method"] = method
    if component_details is not None:
        detail["components"] = component_details

    source_tags = []
    if direct_detail and direct_detail.get("local_name"):
        source_tags.append(direct_detail["local_name"])
    for component in component_details or []:
        tag = component.get("local_name")
        if tag and tag not in source_tags:
            source_tags.append(tag)
    detail["source_tags"] = source_tags
    detail["is_estimated"] = method == "estimated_from_assets"
    return detail


def _add_valuation_share_count(metrics: dict[str, float | None], details: dict[str, dict | None]) -> None:
    issued = _positive_number(metrics.get("issued_shares"))
    treasury = _non_negative_number(metrics.get("treasury_shares"))
    if issued is None or treasury is None:
        metrics["shares_for_valuation"] = None
        details["shares_for_valuation"] = None
        return

    shares = issued - treasury
    if shares <= 0:
        metrics["shares_for_valuation"] = None
        details["shares_for_valuation"] = None
        return

    metrics["shares_for_valuation"] = shares
    details["shares_for_valuation"] = {
        "value": shares,
        "method": "issued_shares_minus_treasury_shares",
        "components": {
            "issued_shares": details.get("issued_shares"),
            "treasury_shares": details.get("treasury_shares"),
        },
    }


def _best_value_for_names(
    facts: list[Fact],
    contexts: dict[str, ContextInfo],
    names: tuple[str, ...],
    period_type: str,
    exclude_nonconsolidated: bool = True,
    allow_share_unit: bool = False,
    context_include: tuple[str, ...] = (),
    context_exclude: tuple[str, ...] = (),
) -> tuple[float | None, dict | None]:
    name_rank = {name: len(names) - index for index, name in enumerate(names)}
    matches = [
        fact
        for fact in facts
        if _normalize_name(fact.local_name) in name_rank
        and fact.value is not None
        and not _is_unwanted_unit(fact.unit_ref, allow_share_unit=allow_share_unit)
        and not (exclude_nonconsolidated and _is_nonconsolidated_context(fact, contexts.get(fact.context_ref or "")))
        and _context_matches(fact, contexts.get(fact.context_ref or ""), context_include, context_exclude)
    ]
    if not matches:
        return None, None

    ranked = sorted(
        matches,
        key=lambda fact: _fact_score(fact, contexts.get(fact.context_ref or ""), period_type)
        + name_rank.get(_normalize_name(fact.local_name), 0),
        reverse=True,
    )
    best = ranked[0]
    context = contexts.get(best.context_ref or "")
    return best.value, {
        "value": best.value,
        "concept": best.concept,
        "local_name": best.local_name,
        "context_ref": best.context_ref,
        "unit_ref": best.unit_ref,
        "source_file": best.source_file,
        "scale": best.scale,
        "decimals": best.decimals,
        "context_period": _context_period(context),
    }


def _fact_score(fact: Fact, context: ContextInfo | None, period_type: str) -> int:
    score = 0
    context_ref = (fact.context_ref or "").lower()
    context_text = (context.raw_text if context else "").lower()
    combined = f"{context_ref} {context_text}"

    if "currentyear" in context_ref or "current" in context_ref:
        score += 50
    if "prioryear" in context_ref or "prior" in context_ref or "previous" in context_ref:
        score -= 80
    if "nonconsolidated" in combined or "non_consolidated" in combined:
        score -= 100
    else:
        score += 15
    if "consolidated" in combined and "nonconsolidated" not in combined:
        score += 15

    if period_type == "duration":
        if context and context.is_duration:
            score += 25
        if "duration" in context_ref:
            score += 10
        if context and context.is_instant:
            score -= 30
    elif period_type == "instant":
        if context and context.is_instant:
            score += 25
        if "instant" in context_ref:
            score += 10
        if context and context.is_duration:
            score -= 30

    if fact.unit_ref and "jpy" in fact.unit_ref.lower():
        score += 5
    if "member" in combined:
        score -= 20
    if "reportablesegmentmember" in combined or "reconcilingitemsmember" in combined:
        score -= 30
    return score


def _has_consolidated_facts(facts: list[Fact], contexts: dict[str, ContextInfo]) -> bool:
    financial_markers = (
        "ifrs",
        "summaryofbusinessresults",
        "cashflowsfromusedinoperatingactivities",
        "profitlossattributabletoownersofparent",
    )
    for fact in facts:
        context = contexts.get(fact.context_ref or "")
        if _is_nonconsolidated_context(fact, context):
            continue
        local_name = fact.local_name.lower()
        if any(marker in local_name for marker in financial_markers):
            return True
    return False


def _is_nonconsolidated_context(fact: Fact, context: ContextInfo | None) -> bool:
    combined = f"{fact.context_ref or ''} {context.raw_text if context else ''}".lower()
    return "nonconsolidated" in combined or "non_consolidated" in combined


def _parse_number(value: str | None, scale: int | None = None, sign: str | None = None) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text or text in {"-", "－", "—", "―"}:
        return None

    negative = False
    if text.startswith(("△", "▲")):
        negative = True
        text = text[1:]
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    if sign == "-":
        negative = True

    text = (
        text.replace(",", "")
        .replace("\u00a0", "")
        .replace(" ", "")
        .replace("百万円", "")
        .replace("千円", "")
        .replace("円", "")
        .replace("%", "")
    )
    text = re.sub(r"[^0-9.+\-]", "", text)
    if not text or text in {"+", "-", ".", "+.", "-."}:
        return None

    try:
        number = float(text)
    except ValueError:
        return None

    if scale is not None:
        number *= 10**scale
    if negative and number > 0:
        number *= -1
    return number


def _attr(tag, attr_name: str) -> str | None:
    lowered = attr_name.lower()
    for key, value in tag.attrs.items():
        if key.lower() == lowered:
            return value[0] if isinstance(value, list) and value else value
    return None


def _child_text(tag, local_name: str) -> str | None:
    child = tag.find(lambda item: item.name and item.name.lower().endswith(local_name.lower()))
    return child.get_text("", strip=True) if child else None


def _first_child_text(element: etree._Element, local_name: str) -> str | None:
    for child in element.iter():
        if _local_name(_element_name(child)).lower() == local_name.lower():
            return "".join(child.itertext()).strip()
    return None


def _context_period(context: ContextInfo | None) -> dict | None:
    if not context:
        return None
    return {
        "start_date": context.start_date,
        "end_date": context.end_date,
        "instant": context.instant,
    }


def _safe_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _element_name(element: etree._Element) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        return etree.QName(tag).localname
    return tag


def _local_name(name: str) -> str:
    return name.split(":")[-1].split("}")[-1]


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _local_name(name).lower())


def _context_matches(
    fact: Fact,
    context: ContextInfo | None,
    include_terms: tuple[str, ...],
    exclude_terms: tuple[str, ...],
) -> bool:
    combined = f"{fact.context_ref or ''} {context.raw_text if context else ''}".lower()
    if include_terms and not all(term.lower() in combined for term in include_terms):
        return False
    if exclude_terms and any(term.lower() in combined for term in exclude_terms):
        return False
    return True


def _positive_number(value: float | None) -> float | None:
    if value is None or value <= 0:
        return None
    return value


def _non_negative_number(value: float | None) -> float | None:
    if value is None or value < 0:
        return None
    return value


def _is_unwanted_unit(unit_ref: str | None, allow_share_unit: bool = False) -> bool:
    if not unit_ref:
        return False
    lowered = unit_ref.lower()
    blocked = ["pure", "percent", "ratio"]
    if not allow_share_unit:
        blocked.append("shares")
    return any(token in lowered for token in blocked)

"""SEC EDGAR data-layer adapter: maps raw companyfacts JSON into the
internal schema (Issue #1), per docs/decisions.md ("EDGAR-Adapter, Issue
#7 Teil 2").

Network calls (resolve_cik, fetch_companyfacts) are kept separate from
map_companyfacts_to_periods()/build_company_record() - the mapping
functions are pure and network-free, so they can be tested directly
against the raw fixtures in tests/fixtures/edgar_raw/ without hitting SEC.
"""

import datetime
import json
import time
import urllib.request

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"

# SEC fair-use rate limiting, per docs/decisions.md (EDGAR-Pilot entry) -
# applies to every request, not just the pilot's.
SEC_REQUEST_DELAY_SECONDS = 1.0

MAX_YEARS = 3

EBIT_TAGS = ("OperatingIncomeLoss",)
TAX_EXPENSE_TAGS = ("IncomeTaxExpenseBenefit",)
PRETAX_INCOME_TAGS = ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",)
DA_TAGS = (
    "DepreciationDepletionAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
    "DepreciationAndAmortization",
    "Depreciation",
)
EQUITY_TAGS = ("StockholdersEquity",)
CASH_TAGS = (
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
)
CFO_TAGS = ("NetCashProvidedByUsedInOperatingActivities",)
NET_INCOME_TAGS = ("NetIncomeLoss", "ProfitLoss")
LONG_TERM_DEBT_NONCURRENT_TAGS = ("LongTermDebtNoncurrent",)
LONG_TERM_DEBT_CURRENT_TAGS = ("LongTermDebtCurrent",)
SHORT_TERM_BORROWINGS_TAGS = ("ShortTermBorrowings",)
REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
)

# data_quality_flags values - set when a lower-priority fallback tag was
# used instead of the preferred one, per docs/decisions.md Entscheidung 4.
DA_INCOMPLETE_FLAG = "da_incomplete_depreciation_only"
NET_INCOME_INCLUDES_NCI_FLAG = "net_income_includes_noncontrolling_interest"
REVENUE_INCLUDING_ASSESSED_TAX_FLAG = "revenue_including_assessed_tax"
CASH_INCLUDES_RESTRICTED_FLAG = "cash_includes_restricted_cash"
DEBT_COMPONENT_MISSING_FLAG = "debt_component_missing_defaulted_zero"


class EdgarMappingError(Exception):
    """Raised when raw companyfacts data cannot be mapped at all.

    Reserved for cases with no sensible fallback (e.g. the EBIT anchor tag
    missing entirely, or a concept with no fallback tag holding any data
    for a period) - never for the debt-component "missing tag = 0" case,
    which is a documented, deliberate default, not an error.
    """


def _is_full_year_duration(point):
    """True unless this point is a quarterly/stub context masquerading as annual.

    SEC's `fp` field means "taken from an annual report", not "spans a
    full year" - a 10-K's XBRL also carries quarterly contexts (e.g. Q4
    alone, or a partial stub period) that can still show up with
    fp="FY"/form="10-K". A point-in-time balance-sheet fact has no
    `start` at all and is exempt (an instant can't be a "stub duration").
    Duration facts (income statement, cash flow) are checked against a
    350-380 day span to reject those quarterly contexts.
    """
    start = point.get("start")
    if start is None:
        return True
    span_days = (datetime.date.fromisoformat(point["end"]) - datetime.date.fromisoformat(start)).days
    return 350 <= span_days <= 380


def _raw_annual_points(tag_facts):
    """period end -> unaggregated list of matching annual (10-K, FY) points.

    Kept unaggregated (not collapsed to one point per end) because value
    and fiscal-year label need different resolutions among duplicates -
    see _annual_points_by_end and _anchor_period_ends.
    """
    usd = tag_facts.get("units", {}).get("USD", [])
    by_end = {}
    for point in usd:
        if not point.get("form", "").startswith("10-K") or point.get("fp") != "FY":
            continue
        if not _is_full_year_duration(point):
            continue
        by_end.setdefault(point["end"], []).append(point)
    return by_end


def _annual_points_by_end(tag_facts):
    """period end -> best USD point for one tag's annual (10-K, FY) data.

    A tag often reports the same fiscal-year end more than once (the same
    value re-appears as a prior-year comparative in later filings, or
    differs after a restatement) - the point with the latest `filed` date
    wins per period end, per docs/decisions.md Entscheidung 2.
    """
    return {
        end: max(points, key=lambda p: p.get("filed", ""))
        for end, points in _raw_annual_points(tag_facts).items()
    }


def _resolve_tag_value(gaap, tags, end):
    """First candidate tag (priority order) with a value at this period end.

    Returns (value, tag_name), or (None, None) if no candidate has data
    for this period end at all.
    """
    for tag in tags:
        facts = gaap.get(tag)
        if facts is None:
            continue
        points = _annual_points_by_end(facts)
        if end in points:
            return points[end]["val"], tag
    return None, None


def _anchor_period_ends(gaap, max_years):
    """The `max_years` most recent (period_end, fiscal_year) pairs, anchored on EBIT.

    OperatingIncomeLoss is the single concept confirmed clean and current
    across the pilot universe (docs/decisions.md) - it determines which
    fiscal years this company even has, before any other concept is
    resolved. Fewer than max_years pairs are returned if the company
    reports a shorter history (analogous to ORBT18's insufficient_years
    case in the dummy fixtures).

    The fiscal_year label is the MINIMUM `fy` value seen across all
    duplicate points for that period end, not derived from the end date's
    calendar year and not just "the fy field" of an arbitrary duplicate:
    SEC's `fy` marks which annual report a data point was cited from, and
    a period's value keeps reappearing (with an incrementing fy) as a
    prior-year comparative in every subsequent 10-K - only the smallest fy
    among the duplicates is the period's own, original fiscal year. A
    company with a non-calendar fiscal year end (e.g. FOSL's FY2025 ending
    2026-01-03) would otherwise be mislabeled by its end date's calendar
    year.
    """
    facts = gaap.get(EBIT_TAGS[0])
    if facts is None:
        raise EdgarMappingError(f"anchor tag {EBIT_TAGS[0]!r} missing entirely - cannot determine fiscal years")
    raw = _raw_annual_points(facts)
    if not raw:
        raise EdgarMappingError(f"anchor tag {EBIT_TAGS[0]!r} has no annual (10-K/FY) data points")
    ends_desc = sorted(raw.keys(), reverse=True)[:max_years]
    return [(end, min(p["fy"] for p in raw[end])) for end in sorted(ends_desc)]


def map_companyfacts_to_periods(raw_companyfacts, max_years=MAX_YEARS):
    """Map raw SEC companyfacts JSON into a list of internal-schema periods.

    Pure/network-free - operates only on already-fetched JSON, so it is
    testable directly against tests/fixtures/edgar_raw/*.json. Applies the
    tag-fallback rules from docs/decisions.md: missing debt-component tags
    default to 0 and set data_quality_flags (not an error - Entscheidung 5),
    pretax income <= 0 yields tax_rate=None (triggers metrics_layer's
    undefined_tax_rate guard), and falling back to a lower-priority tag for
    D&A/NetIncome/Revenue sets the matching data_quality_flags entry.

    Raises EdgarMappingError if a concept with no accepted "missing = 0"
    default (i.e. anything other than the three debt components) has no
    data under any of its candidate tags for a period.
    """
    gaap = raw_companyfacts.get("facts", {}).get("us-gaap", {})
    period_ends = _anchor_period_ends(gaap, max_years)

    periods = []
    for end, fiscal_year in period_ends:
        flags = []

        ebit, _ = _resolve_tag_value(gaap, EBIT_TAGS, end)
        if ebit is None:
            raise EdgarMappingError(f"EBIT missing for period end {end}")

        da_value, da_tag = _resolve_tag_value(gaap, DA_TAGS, end)
        if da_value is None:
            raise EdgarMappingError(f"D&A missing for period end {end}")
        if da_tag == DA_TAGS[-1]:
            flags.append(DA_INCOMPLETE_FLAG)
        ebitda = ebit + da_value

        tax_expense, _ = _resolve_tag_value(gaap, TAX_EXPENSE_TAGS, end)
        pretax_income, _ = _resolve_tag_value(gaap, PRETAX_INCOME_TAGS, end)
        if tax_expense is None or pretax_income is None:
            raise EdgarMappingError(f"tax components missing for period end {end}")
        tax_rate = None if pretax_income <= 0 else tax_expense / pretax_income

        equity, _ = _resolve_tag_value(gaap, EQUITY_TAGS, end)
        if equity is None:
            raise EdgarMappingError(f"equity missing for period end {end}")

        cash, cash_tag = _resolve_tag_value(gaap, CASH_TAGS, end)
        if cash is None:
            raise EdgarMappingError(f"cash missing for period end {end}")
        if cash_tag == CASH_TAGS[-1]:
            flags.append(CASH_INCLUDES_RESTRICTED_FLAG)

        long_term_noncurrent, _ = _resolve_tag_value(gaap, LONG_TERM_DEBT_NONCURRENT_TAGS, end)
        long_term_current, _ = _resolve_tag_value(gaap, LONG_TERM_DEBT_CURRENT_TAGS, end)
        short_term_borrowings, _ = _resolve_tag_value(gaap, SHORT_TERM_BORROWINGS_TAGS, end)
        if long_term_noncurrent is None or long_term_current is None or short_term_borrowings is None:
            flags.append(DEBT_COMPONENT_MISSING_FLAG)
        total_debt = (long_term_noncurrent or 0) + (long_term_current or 0) + (short_term_borrowings or 0)
        net_debt = total_debt - cash

        cfo, _ = _resolve_tag_value(gaap, CFO_TAGS, end)
        if cfo is None:
            raise EdgarMappingError(f"cfo missing for period end {end}")

        net_income, net_income_tag = _resolve_tag_value(gaap, NET_INCOME_TAGS, end)
        if net_income is None:
            raise EdgarMappingError(f"net income missing for period end {end}")
        if net_income_tag == NET_INCOME_TAGS[-1]:
            flags.append(NET_INCOME_INCLUDES_NCI_FLAG)

        revenue, revenue_tag = _resolve_tag_value(gaap, REVENUE_TAGS, end)
        if revenue is None:
            raise EdgarMappingError(f"revenue missing for period end {end}")
        if revenue_tag == REVENUE_TAGS[-1]:
            flags.append(REVENUE_INCLUDING_ASSESSED_TAX_FLAG)

        periods.append(
            {
                "fiscal_year": fiscal_year,
                "revenue": revenue,
                "ebit": ebit,
                "ebitda": ebitda,
                "tax_rate": tax_rate,
                "equity": equity,
                "net_debt": net_debt,
                "net_income": net_income,
                "cfo": cfo,
                "data_quality_flags": flags,
            }
        )
    return periods


def build_company_record(ticker, raw_companyfacts, max_years=MAX_YEARS, currency="USD"):
    """Wrap map_companyfacts_to_periods() into the full internal schema shape.

    sector is left as None - EDGAR's SIC codes are explicitly rejected as
    a classification source (docs/masterplan.md §4), and no replacement
    (NAICS/price-feed sector) is wired up yet.
    """
    return {
        "company_id": ticker,
        "name": raw_companyfacts.get("entityName", ticker),
        "sector": None,
        "currency": currency,
        "periods": map_companyfacts_to_periods(raw_companyfacts, max_years=max_years),
    }


# resolve_cik()/fetch_companyfacts() have no retry logic for transient
# network failures - relevant for cron operation (Issue #9/#10), deliberately
# not built here in Issue #7.
def resolve_cik(ticker, user_agent, ticker_map=None):
    """Look up a ticker's 10-digit zero-padded CIK.

    Uses the official SEC company_tickers.json (docs/decisions.md - CIKs
    are never guessed). Pass an already-fetched ticker_map (as returned by
    json-decoding that file) to avoid re-fetching it for every ticker in a
    batch; otherwise it is fetched fresh. Network call unless ticker_map
    is given.
    """
    if ticker_map is None:
        req = urllib.request.Request(SEC_TICKERS_URL, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req) as resp:
            ticker_map = json.load(resp)

    ticker_upper = ticker.upper()
    for entry in ticker_map.values():
        if entry["ticker"].upper() == ticker_upper:
            return str(entry["cik_str"]).zfill(10)
    raise EdgarMappingError(f"ticker {ticker!r} not found in company_tickers.json")


def fetch_companyfacts(cik10, user_agent):
    """Fetch raw companyfacts JSON for a 10-digit zero-padded CIK. Network call."""
    url = SEC_COMPANYFACTS_URL.format(cik10=cik10)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def fetch_company_record(ticker, user_agent, max_years=MAX_YEARS, ticker_map=None):
    """Resolve CIK, fetch companyfacts, and map one ticker to an internal-schema record.

    Network calls, rate-limited per docs/decisions.md (SEC fair-use) - the
    delay sits between the CIK lookup and the companyfacts fetch for THIS
    ticker only. Issue #8 (full fetch over 20-30 tickers): the caller must
    add its own delay BETWEEN tickers and pass a shared, already-fetched
    ticker_map into resolve_cik instead of re-fetching it per ticker -
    otherwise 20-30 back-to-back companies risk violating SEC's fair-use
    policy.
    """
    cik10 = resolve_cik(ticker, user_agent, ticker_map=ticker_map)
    time.sleep(SEC_REQUEST_DELAY_SECONDS)
    raw = fetch_companyfacts(cik10, user_agent)
    return build_company_record(ticker, raw, max_years=max_years)

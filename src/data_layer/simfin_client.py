"""SimFin data-layer adapter: maps raw companies/statements/compact JSON into
the internal schema (Issue #1), per docs/decisions.md ("SimFin-Client, Issue
#8").

Network calls (resolve_simfin_id, fetch_statements_batch) are kept separate
from map_simfin_to_periods()/build_company_record() - the mapping functions
are pure and network-free, so they can be tested directly against the raw
fixtures in tests/fixtures/simfin_raw/ without hitting the SimFin API.
"""

import json
import time
import urllib.error
import urllib.request

SIMFIN_BASE_URL = "https://backend.simfin.com/api/v3"
SIMFIN_COMPANIES_LIST_URL = f"{SIMFIN_BASE_URL}/companies/list"
SIMFIN_STATEMENTS_URL = f"{SIMFIN_BASE_URL}/companies/statements/compact"

MAX_YEARS = 3

# Empirically confirmed limit (docs/decisions.md, "SimFin-Sondierung"): 3+
# ids in one companies/statements/compact call fails with a 500 "Exceeded
# number of companies allowed". The number of simultaneously requested
# statement types is unrelated and not limited the same way.
MAX_BATCH_SIZE = 2

# HTTP 429 ("You have exhausted your API Request Quota") never carries a
# Retry-After header (docs/decisions.md) - a fixed, empirically-chosen delay
# is the best available safety net, not a computed backoff.
RETRY_DELAY_SECONDS = 90
MAX_RETRY_ATTEMPTS = 12

# data_quality_flags values, per docs/decisions.md ("SimFin-Client"
# Entscheidungen 5/6). debt_component_missing_defaulted_zero shares its exact
# string with edgar_client.py's DEBT_COMPONENT_MISSING_FLAG (same meaning in
# the internal schema, independent adapter modules - no cross-import).
DEBT_COMPONENT_MISSING_FLAG = "debt_component_missing_defaulted_zero"
DEBT_STATEMENT_MISMATCH_FLAG = "debt_statement_mismatch"
NET_DEBT_UNRELIABLE_FLAG = "net_debt_unreliable"

# Reliability threshold for the net_debt guard below - docs/decisions.md
# ("SimFin-Client" Entscheidung 6).
NET_DEBT_UNRELIABLE_RATIO = 0.10


class SimFinMappingError(Exception):
    """Raised when raw SimFin statements data cannot be mapped at all.

    Reserved for a company with no usable EBIT/anchor data whatsoever. A
    single fiscal year missing some other required field is deliberately
    NOT an error - it just drops that year from the returned periods
    (docs/decisions.md Entscheidung 5, "SimFin-Client" - a considered
    difference from edgar_client.py's per-field crash, since such gaps are
    common in SimFin's data, not rare anomalies).
    """


def _column_index(columns, name):
    return columns.index(name)


def _statement(raw_company, statement_code):
    for stmt in raw_company.get("statements", []):
        if stmt["statement"] == statement_code:
            return stmt
    return None


def _rows_by_fiscal_year(stmt, fiscal_period):
    """fiscal_year -> row, for rows matching the given Fiscal Period label.

    No duplicate (Fiscal Period, Fiscal Year) pairs were observed in any of
    the 23 committed pilot fixtures (docs/decisions.md) - if SimFin ever
    returns one, the later row in `data` silently wins (dict overwrite).
    """
    cols = stmt["columns"]
    fp_idx = _column_index(cols, "Fiscal Period")
    fy_idx = _column_index(cols, "Fiscal Year")
    return {row[fy_idx]: row for row in stmt["data"] if row[fp_idx] == fiscal_period}


def map_simfin_to_periods(raw_company, max_years=MAX_YEARS):
    """Map one company's raw SimFin statements JSON into internal-schema periods.

    Pure/network-free - operates only on already-fetched JSON (one entry of
    a companies/statements/compact response), so it is testable directly
    against tests/fixtures/simfin_raw/*.json.

    Anchored on PL's "Operating Income (Loss)" (Fiscal Period "FY"), taking
    the `max_years` most recent fiscal years THIS company itself reports -
    not the most recent calendar years relative to today (docs/decisions.md
    Entscheidung 4, same philosophy as edgar_client.py's EBIT anchoring).

    A fiscal year missing any other required field (revenue, net_income,
    D&A, cfo, equity, cash, total_assets - none of which have a fallback
    source) is dropped from the result entirely, rather than raising
    (docs/decisions.md Entscheidung 5) - this can leave fewer than
    max_years periods, which the Stage-1 metrics' existing
    insufficient_years guard already handles.

    Short Term Debt/Long Term Debt default to 0 when missing, flagged
    debt_component_missing_defaulted_zero (docs/decisions.md Entscheidung
    5). net_debt itself is set to None ("net_debt_unreliable") whenever
    EBITDA <= 0 or the fiscal year's debt-related financing cash flow is
    large relative to EBITDA (docs/decisions.md Entscheidung 6) - otherwise
    net_debt is the normal balance-sheet figure, with an informational
    debt_statement_mismatch flag if that cash flow is nonzero.

    Raises SimFinMappingError if there is no PL statement at all, or no
    fiscal year has a non-null anchor value.
    """
    pl = _statement(raw_company, "PL")
    if pl is None:
        raise SimFinMappingError("no PL statement in raw data - cannot anchor fiscal years")

    pl_cols = pl["columns"]
    ebit_idx = _column_index(pl_cols, "Operating Income (Loss)")
    revenue_idx = _column_index(pl_cols, "Revenue")
    net_income_idx = _column_index(pl_cols, "Net Income")
    pl_fy_rows = _rows_by_fiscal_year(pl, "FY")

    anchor_years = sorted(
        (year for year, row in pl_fy_rows.items() if row[ebit_idx] is not None),
        reverse=True,
    )[:max_years]
    if not anchor_years:
        raise SimFinMappingError("no fiscal year has a non-null Operating Income (Loss) - cannot anchor")

    cf = _statement(raw_company, "CF")
    bs = _statement(raw_company, "BS")
    cf_fy_rows = _rows_by_fiscal_year(cf, "FY") if cf else {}
    bs_q4_rows = _rows_by_fiscal_year(bs, "Q4") if bs else {}

    da_idx = _column_index(cf["columns"], "Depreciation & Amortization") if cf else None
    cfo_idx = _column_index(cf["columns"], "Cash from Operating Activities") if cf else None
    debt_cf_idx = _column_index(cf["columns"], "Cash From (Repayment of) Debt") if cf else None

    equity_idx = _column_index(bs["columns"], "Total Equity") if bs else None
    cash_idx = _column_index(bs["columns"], "Cash & Cash Equivalents") if bs else None
    st_debt_idx = _column_index(bs["columns"], "Short Term Debt") if bs else None
    lt_debt_idx = _column_index(bs["columns"], "Long Term Debt") if bs else None
    total_assets_idx = _column_index(bs["columns"], "Total Assets") if bs else None

    periods = []
    for year in sorted(anchor_years):
        pl_row = pl_fy_rows[year]
        cf_row = cf_fy_rows.get(year)
        bs_row = bs_q4_rows.get(year)

        ebit = pl_row[ebit_idx]
        revenue = pl_row[revenue_idx]
        net_income = pl_row[net_income_idx]
        da = cf_row[da_idx] if cf_row else None
        cfo = cf_row[cfo_idx] if cf_row else None
        equity = bs_row[equity_idx] if bs_row else None
        cash = bs_row[cash_idx] if bs_row else None
        total_assets = bs_row[total_assets_idx] if bs_row else None

        if any(v is None for v in (revenue, net_income, da, cfo, equity, cash, total_assets)):
            continue  # required field missing, no fallback - drop this year (Entscheidung 5)

        flags = []
        st_debt = bs_row[st_debt_idx]
        lt_debt = bs_row[lt_debt_idx]
        if st_debt is None or lt_debt is None:
            flags.append(DEBT_COMPONENT_MISSING_FLAG)
        total_debt = (st_debt or 0) + (lt_debt or 0)

        ebitda = ebit + da
        debt_cf = (cf_row[debt_cf_idx] if cf_row else None) or 0
        unreliable = ebitda <= 0 or (abs(debt_cf) / abs(ebitda)) >= NET_DEBT_UNRELIABLE_RATIO
        if unreliable:
            net_debt = None
            flags.append(NET_DEBT_UNRELIABLE_FLAG)
        else:
            net_debt = total_debt - cash
            if debt_cf != 0:
                flags.append(DEBT_STATEMENT_MISMATCH_FLAG)

        periods.append(
            {
                "fiscal_year": year,
                "revenue": revenue,
                "ebit": ebit,
                "ebitda": ebitda,
                "tax_rate": None,
                "equity": equity,
                "net_debt": net_debt,
                "total_assets": total_assets,
                "net_income": net_income,
                "cfo": cfo,
                "data_quality_flags": flags,
            }
        )
    return periods


def build_company_record(ticker, raw_company, max_years=MAX_YEARS, currency="USD"):
    """Wrap map_simfin_to_periods() into the full internal schema shape.

    sector is left as None, same as edgar_client.py - no classification
    source is wired up yet (docs/masterplan.md §4).

    Only "USD" is supported, same rationale and same guard as
    edgar_client.py's build_company_record: no non-USD data source or
    conversion exists in this project.
    """
    if currency != "USD":
        raise SimFinMappingError(f"unsupported currency {currency!r} - only USD is supported")
    return {
        "company_id": ticker,
        "name": raw_company.get("name", ticker),
        "sector": None,
        "currency": currency,
        "periods": map_simfin_to_periods(raw_company, max_years=max_years),
    }


def resolve_simfin_id(ticker, api_key, company_list=None):
    """Look up a ticker's SimFin company id.

    companies/list ignores ticker/tickers/search filter params entirely
    (docs/decisions.md, "SimFin-Wechsel") - the full company list must be
    fetched and filtered client-side. Pass an already-fetched company_list
    (as returned by json-decoding that endpoint) to avoid re-fetching it for
    every ticker in a batch; otherwise it is fetched fresh. Network call
    unless company_list is given.
    """
    if company_list is None:
        req = urllib.request.Request(
            SIMFIN_COMPANIES_LIST_URL, headers={"Authorization": api_key, "Accept": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            company_list = json.load(resp)

    ticker_upper = ticker.upper()
    for entry in company_list:
        if entry.get("ticker", "").upper() == ticker_upper:
            return entry["id"]
    raise SimFinMappingError(f"ticker {ticker!r} not found in companies/list")


def fetch_statements_batch(ids, api_key, statements=("PL", "BS", "CF")):
    """Fetch bundled statements for up to MAX_BATCH_SIZE company ids in one call.

    Network call. Retries up to MAX_RETRY_ATTEMPTS times on HTTP 429 ("quota
    exhausted"), waiting RETRY_DELAY_SECONDS between attempts - a fixed
    delay, not a computed backoff, since SimFin never returns a Retry-After
    header to size one from (docs/decisions.md).
    """
    if len(ids) > MAX_BATCH_SIZE:
        raise ValueError(f"at most {MAX_BATCH_SIZE} ids per batch call, got {len(ids)}")

    url = f"{SIMFIN_STATEMENTS_URL}?id={','.join(str(i) for i in ids)}&statements={','.join(statements)}"
    req = urllib.request.Request(url, headers={"Authorization": api_key, "Accept": "application/json"})

    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < MAX_RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            raise

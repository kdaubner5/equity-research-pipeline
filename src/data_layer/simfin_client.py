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

# Per-request socket timeout - without one a stalled connection would block
# a cron batch forever (code review of PR #30). Generous, since one batch
# call bundles PL/BS/CF for two companies.
REQUEST_TIMEOUT_SECONDS = 60

# data_quality_flags values, per docs/decisions.md ("SimFin-Client"
# Entscheidung 5 and "net_debt-Zuverlässigkeit - Stock-Flow-Abgleich").
# debt_component_missing_defaulted_zero shares its exact string with
# edgar_client.py's DEBT_COMPONENT_MISSING_FLAG (same meaning in the internal
# schema, independent adapter modules - no cross-import).
DEBT_COMPONENT_MISSING_FLAG = "debt_component_missing_defaulted_zero"
# net_debt is None because the balance-sheet debt change contradicts the
# reported debt cash flow beyond tolerance, or could not be checked at all
# (no prior-year balance sheet). Always accompanied by the generic
# net_debt_unreliable, which is what metrics_layer reports.
DEBT_STOCK_FLOW_MISMATCH_FLAG = "debt_stock_flow_mismatch"
DEBT_STOCK_FLOW_UNVERIFIABLE_FLAG = "debt_stock_flow_unverifiable"
NET_DEBT_UNRELIABLE_FLAG = "net_debt_unreliable"
# Both BS debt components AND the debt cash flow are null (not 0) - no debt
# information at all, so not verified debt-free (Entscheidung 10).
DEBT_DATA_COMPLETELY_MISSING_FLAG = "debt_data_completely_missing"
# BS/CF reach a fiscal year later than the newest PL year the adapter
# anchored on - informative only, never an invalid_reason (Entscheidung 11).
PL_LAGS_OTHER_STATEMENTS_FLAG = "pl_lags_other_statements"
# At least one anchored year was dropped for a missing required value
# (Entscheidung 14) - tells a data gap (this) apart from a genuinely short
# history (fewer EBIT-having years exist at all, no flag).
YEAR_DROPPED_MISSING_FIELD_FLAG = "year_dropped_missing_required_field"

# Tolerance of the stock-flow reconciliation (balance-sheet debt change vs.
# reported debt cash flow) - docs/decisions.md, "net_debt-Zuverlässigkeit -
# Stock-Flow-Abgleich", Entscheidung 2: a year reconciles if
#   |delta_debt - debt_cf| <= max(RATIO * max(debt_t, debt_t-1),
#                                 ASSETS_RATIO * total_assets_t)
# Module constants, not YAML: config-driven thresholds are Phase 1.1
# (docs/masterplan.md §7), same Phase-1.0 exception as the Gates-Layer
# thresholds (SimFin-Client Entscheidung 13). Fixed before the gates
# distribution was recomputed - do not tune them to that result.
DEBT_RECONCILIATION_TOLERANCE_RATIO = 0.05
DEBT_RECONCILIATION_TOLERANCE_ASSETS_RATIO = 0.001


class SimFinMappingError(Exception):
    """Raised when raw SimFin statements data cannot be mapped at all.

    Reserved for a company with no usable EBIT/anchor data whatsoever, or
    raw data whose structure is not what the mapping assumes (an expected
    column absent, duplicate (Fiscal Period, Fiscal Year) rows, a ticker or
    currency that does not match what was requested). A single fiscal year
    missing some other required VALUE is deliberately NOT an error - it
    just drops that year from the returned periods (docs/decisions.md
    Entscheidungen 5 and 12, "SimFin-Client" - a considered difference from
    edgar_client.py's per-field crash, since such gaps are common in
    SimFin's data, not rare anomalies).
    """


def _column_index(columns, name):
    try:
        return columns.index(name)
    except ValueError:
        raise SimFinMappingError(f"expected column {name!r} is missing from the statement's columns") from None


def _statement(raw_company, statement_code):
    for stmt in raw_company.get("statements", []):
        if stmt["statement"] == statement_code:
            return stmt
    return None


def _rows_by_fiscal_year(stmt, fiscal_period):
    """fiscal_year -> row, for rows matching the given Fiscal Period label.

    No duplicate (Fiscal Period, Fiscal Year) pairs were observed in any of
    the 23 committed pilot fixtures (docs/decisions.md) - but that says
    nothing about the full universe, and picking either of two conflicting
    rows would be a silent guess, so a duplicate raises SimFinMappingError.
    """
    cols = stmt["columns"]
    fp_idx = _column_index(cols, "Fiscal Period")
    fy_idx = _column_index(cols, "Fiscal Year")
    rows = {}
    for row in stmt["data"]:
        if row[fp_idx] != fiscal_period:
            continue
        fiscal_year = row[fy_idx]
        if fiscal_year in rows:
            raise SimFinMappingError(
                f"duplicate ({fiscal_period!r}, {fiscal_year}) rows in the {stmt['statement']} statement - "
                "ambiguous, refusing to pick one"
            )
        rows[fiscal_year] = row
    return rows


def _debt_stock_flow_reconciles(debt_now, debt_prev, debt_cf, total_assets):
    """True if the balance-sheet debt change agrees with the reported debt cash flow.

    Tolerance per DEBT_RECONCILIATION_TOLERANCE_* above: a share of the debt
    level (non-cash movements - FX translation, discount amortization,
    finance-lease additions - scale with the debt stock, not with the size of
    the cash flow). The relative tolerance is degenerate at debt level exactly
    0 (any nonzero cash flow would be an "infinite" relative discrepancy), so
    that one case falls back to an absolute floor, a share of total assets
    (code review of this branch, confirmed against the KELYA FY2020 fixture:
    combining both terms via max() - the pre-fix behaviour - let the assets
    floor of an asset-heavy company swamp a real, ~110%-of-debt-level
    contradiction whenever debt was merely small relative to that company's
    balance sheet, not actually zero; every "rescued by the floor" case in the
    23 pilots besides KELYA has debt exactly 0 in both years, so restricting
    the floor to that case changes no other pilot outcome).
    """
    discrepancy = abs((debt_now - debt_prev) - debt_cf)
    debt_level = max(debt_now, debt_prev)
    if debt_level == 0:
        tolerance = DEBT_RECONCILIATION_TOLERANCE_ASSETS_RATIO * total_assets
    else:
        tolerance = DEBT_RECONCILIATION_TOLERANCE_RATIO * debt_level
    return discrepancy <= tolerance


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
    insufficient_years guard already handles. The drop is not silent: every
    returned period carries year_dropped_missing_required_field
    (Entscheidung 14), which tells a data gap apart from a genuinely short
    history (no flag). A company losing ALL years has no period to carry it.

    Short Term Debt/Long Term Debt default to 0 when missing, flagged
    debt_component_missing_defaulted_zero (docs/decisions.md Entscheidung
    5). net_debt is None (flagged debt_data_completely_missing) when both
    debt components AND the debt cash flow are all null - no debt
    information at all is not verified debt-freedom (SimFin-Client
    Entscheidung 10). Otherwise the year's balance-sheet debt change
    (Q4(t) vs Q4(t-1)) is reconciled against its reported debt cash flow
    (null counts as 0): net_debt is the normal balance-sheet figure if they
    agree within tolerance - regardless of how large the movement is
    relative to EBITDA, and regardless of EBITDA's sign - and None
    (debt_stock_flow_mismatch + net_debt_unreliable) if they do not, or
    (debt_stock_flow_unverifiable + net_debt_unreliable) if there is no
    prior-year balance sheet to reconcile against ("net_debt-Zuverlässigkeit
    - Stock-Flow-Abgleich" Entscheidungen 1-3).

    If BS or CF reach a fiscal year later than the newest anchored PL year,
    every returned period carries an informative pl_lags_other_statements
    flag (Entscheidung 11) - never an invalid_reason.

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

    # Explicit check after anchoring (Entscheidung 11): does BS or CF reach a
    # fiscal year later than the newest one PL/EBIT anchored on? Informative
    # flag on every returned period, not an invalid_reason.
    latest_anchor_year = anchor_years[0]
    pl_lags_other_statements = any(
        rows and max(rows) > latest_anchor_year for rows in (cf_fy_rows, bs_q4_rows)
    )

    da_idx = _column_index(cf["columns"], "Depreciation & Amortization") if cf else None
    cfo_idx = _column_index(cf["columns"], "Cash from Operating Activities") if cf else None
    debt_cf_idx = _column_index(cf["columns"], "Cash From (Repayment of) Debt") if cf else None

    equity_idx = _column_index(bs["columns"], "Total Equity") if bs else None
    cash_idx = _column_index(bs["columns"], "Cash & Cash Equivalents") if bs else None
    st_debt_idx = _column_index(bs["columns"], "Short Term Debt") if bs else None
    lt_debt_idx = _column_index(bs["columns"], "Long Term Debt") if bs else None
    total_assets_idx = _column_index(bs["columns"], "Total Assets") if bs else None

    periods = []
    any_year_dropped = False
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
            any_year_dropped = True  # required field missing, no fallback - drop this year (Entscheidung 5), but not silently
            continue

        flags = []
        st_debt = bs_row[st_debt_idx]
        lt_debt = bs_row[lt_debt_idx]
        debt_cf_raw = cf_row[debt_cf_idx]
        current_component_missing = st_debt is None or lt_debt is None
        if current_component_missing:
            flags.append(DEBT_COMPONENT_MISSING_FLAG)
        total_debt = (st_debt or 0) + (lt_debt or 0)

        ebitda = ebit + da  # informational for net_debt: its sign no longer decides reliability

        # A reported 0 is a statement about the company, null is not: all
        # three debt fields null at once is "no information", not "no debt"
        # (SimFin-Client Entscheidung 10) - the stock-flow reconciliation
        # below cannot see it, since it needs at least one side to compare.
        debt_data_missing = st_debt is None and lt_debt is None and debt_cf_raw is None

        if debt_data_missing:
            flags.append(DEBT_DATA_COMPLETELY_MISSING_FLAG)
            net_debt = None
        else:
            prev_bs_row = bs_q4_rows.get(year - 1)
            if prev_bs_row is None:
                flags.extend([DEBT_STOCK_FLOW_UNVERIFIABLE_FLAG, NET_DEBT_UNRELIABLE_FLAG])
                net_debt = None
            else:
                prev_st_debt = prev_bs_row[st_debt_idx]
                prev_lt_debt = prev_bs_row[lt_debt_idx]
                # The reconciliation's own baseline (year t-1) can have the
                # same missing-component gap as year t - defaulting it to 0
                # without a flag would make the check look more certain than
                # it is (code review of this branch; CLAUDE.md: no silent
                # shortcuts on data integrity). Flagged the same way as the
                # current year's default, but - unlike the current year - it
                # does not by itself change net_debt: the reconciliation still
                # runs against the defaulted value, same as decisions.md's own
                # worked ROCK/SUPN FY2023 example (`not current_component_missing`
                # only avoids appending the same flag string twice).
                if not current_component_missing and (prev_st_debt is None or prev_lt_debt is None):
                    flags.append(DEBT_COMPONENT_MISSING_FLAG)
                prev_debt = (prev_st_debt or 0) + (prev_lt_debt or 0)
                if _debt_stock_flow_reconciles(total_debt, prev_debt, debt_cf_raw or 0, total_assets):
                    net_debt = total_debt - cash
                else:
                    flags.extend([DEBT_STOCK_FLOW_MISMATCH_FLAG, NET_DEBT_UNRELIABLE_FLAG])
                    net_debt = None

        if pl_lags_other_statements:
            flags.append(PL_LAGS_OTHER_STATEMENTS_FLAG)

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

    if any_year_dropped:
        for period in periods:
            period["data_quality_flags"].append(YEAR_DROPPED_MISSING_FIELD_FLAG)
    return periods


def build_company_record(ticker, raw_company, max_years=MAX_YEARS, currency="USD"):
    """Wrap map_simfin_to_periods() into the full internal schema shape.

    sector is left as None, same as edgar_client.py - no classification
    source is wired up yet (docs/masterplan.md §4).

    Only "USD" is supported, same rationale and same guard as
    edgar_client.py's build_company_record: no non-USD data source or
    conversion exists in this project. The guard checks both the requested
    currency and, where the raw record states one, the raw record's own
    currency - checking only the argument would label EUR figures "USD".
    Likewise a raw record whose own ticker differs from the requested one
    (fetch_statements_batch returns a bare list, so a mispaired record is
    possible) is refused rather than filed under the wrong ticker.
    """
    if currency != "USD":
        raise SimFinMappingError(f"unsupported currency {currency!r} - only USD is supported")
    raw_currency = raw_company.get("currency")
    if raw_currency is not None and raw_currency != currency:
        raise SimFinMappingError(f"raw record for {ticker!r} is in {raw_currency!r}, only {currency!r} is supported")
    raw_ticker = raw_company.get("ticker")
    if raw_ticker is not None and raw_ticker.upper() != ticker.upper():
        raise SimFinMappingError(f"raw record is for ticker {raw_ticker!r}, but {ticker!r} was requested")
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
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            company_list = json.load(resp)

    ticker_upper = ticker.upper()
    for entry in company_list:
        # `or ""`: an entry with a null ticker (key present, value None) must
        # be skipped, not crash every lookup that scans past it.
        if (entry.get("ticker") or "").upper() == ticker_upper:
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
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < MAX_RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            raise

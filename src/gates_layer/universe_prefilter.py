"""Universe pre-filter (Issue #39, docs/decisions.md "Phase 2.0a", masterplan.md
§4 "Universums-Vorfilter"): market cap, median dollar volume and price
freshness, decided per company as a pure function of already-fetched data.

evaluate_prefilter() always computes market cap, median dollar volume and the
price date as far as the data allows - independent of the exclusion reason,
which only follows the precedence below. The archive keeps these values for
excluded companies too, so thresholds can be re-judged later without new
calls.

Boundary convention: exactly on a threshold = passed (market cap >= min and
<= max, median dollar volume >= min, price age <= max).

Precedence (first applicable reason wins):
  no_classification, no_price_data, price_stale, no_shares,
  market_cap_below_min, market_cap_above_max, insufficient_price_history,
  median_dollar_volume_below_min.
price_fetch_failed and not_fetched are assigned by the runner (the price
request itself did not complete) via runner_excluded(), outside this order.
"""

from dataclasses import dataclass
from datetime import date
from statistics import median

from config_layer.thresholds import PrefilterThresholds

# status
PASSED = "passed"
EXCLUDED = "excluded"

# reasons decided by evaluate_prefilter(), in precedence order
NO_CLASSIFICATION = "no_classification"
NO_PRICE_DATA = "no_price_data"
PRICE_STALE = "price_stale"
NO_SHARES = "no_shares"
MARKET_CAP_BELOW_MIN = "market_cap_below_min"
MARKET_CAP_ABOVE_MAX = "market_cap_above_max"
INSUFFICIENT_PRICE_HISTORY = "insufficient_price_history"
MEDIAN_DOLLAR_VOLUME_BELOW_MIN = "median_dollar_volume_below_min"

# reasons assigned by the runner, not by evaluate_prefilter()
PRICE_FETCH_FAILED = "price_fetch_failed"
NOT_FETCHED = "not_fetched"

RUNNER_REASONS = (PRICE_FETCH_FAILED, NOT_FETCHED)
PREFILTER_REASONS = (
    NO_CLASSIFICATION,
    NO_PRICE_DATA,
    PRICE_STALE,
    NO_SHARES,
    MARKET_CAP_BELOW_MIN,
    MARKET_CAP_ABOVE_MAX,
    INSUFFICIENT_PRICE_HISTORY,
    MEDIAN_DOLLAR_VOLUME_BELOW_MIN,
) + RUNNER_REASONS


@dataclass(frozen=True)
class PrefilterResult:
    simfin_id: int
    ticker: str | None
    sector_code: int | None
    status: str  # PASSED / EXCLUDED
    reason: str | None  # None iff PASSED
    market_cap_usd: float | None
    median_dollar_volume_usd: float | None
    median_window_rows: int | None  # rows that went into the median; None if no row qualified
    price_date: date | None


def _has_price(row):
    return row.close is not None and row.close > 0


def _has_price_and_volume(row):
    return _has_price(row) and row.volume is not None and row.volume >= 0


def evaluate_prefilter(entry, price_rows, *, as_of, thresholds: PrefilterThresholds):
    """Pre-filter one company.

    `entry` needs simfin_id, ticker, sector_code and has_classification
    (data_layer.simfin_universe.UniverseEntry); `price_rows` is a sequence of
    rows with date/close/volume/shares (PriceRow), or None/empty when the
    company has no price data. A row counts as priced only with a positive
    close, and toward the median only with a non-negative volume as well
    (volume 0 is a real trading day and counts).

    Raises ValueError for a row dated after as_of - that is a caller bug (the
    loader requests prices only up to as_of), not company data.
    """
    rows = sorted(price_rows or (), key=lambda r: r.date)
    if any(row.date > as_of for row in rows):
        raise ValueError("price rows dated after as_of - the price request window ends at as_of")

    priced = [row for row in rows if _has_price(row)]
    price_date = priced[-1].date if priced else None

    market_cap = None
    shares_ok = False
    if priced:
        last = priced[-1]
        shares_ok = last.shares is not None and last.shares > 0
        if shares_ok:
            market_cap = last.close * last.shares

    window_rows = [row for row in rows if _has_price_and_volume(row)][-thresholds.adv_window_trading_days :]
    median_dollar_volume = median(row.close * row.volume for row in window_rows) if window_rows else None
    median_window_rows = len(window_rows) if window_rows else None

    reason = _first_reason(
        entry, priced, price_date, shares_ok, market_cap, median_dollar_volume, median_window_rows, as_of, thresholds
    )
    return PrefilterResult(
        simfin_id=entry.simfin_id,
        ticker=entry.ticker,
        sector_code=entry.sector_code,
        status=PASSED if reason is None else EXCLUDED,
        reason=reason,
        market_cap_usd=market_cap,
        median_dollar_volume_usd=median_dollar_volume,
        median_window_rows=median_window_rows,
        price_date=price_date,
    )


def _first_reason(
    entry, priced, price_date, shares_ok, market_cap, median_dollar_volume, median_window_rows, as_of, thresholds
):
    if not entry.has_classification:
        return NO_CLASSIFICATION
    if not priced:
        return NO_PRICE_DATA
    if (as_of - price_date).days > thresholds.max_price_age_days:
        return PRICE_STALE
    if not shares_ok:
        return NO_SHARES
    if market_cap < thresholds.min_market_cap_usd:
        return MARKET_CAP_BELOW_MIN
    if market_cap > thresholds.max_market_cap_usd:
        return MARKET_CAP_ABOVE_MAX
    if median_window_rows is None or median_window_rows < thresholds.adv_window_trading_days:
        return INSUFFICIENT_PRICE_HISTORY
    if median_dollar_volume < thresholds.min_median_dollar_volume_usd:
        return MEDIAN_DOLLAR_VOLUME_BELOW_MIN
    return None


def runner_excluded(entry, reason):
    """A result for a company whose price request did not complete
    (price_fetch_failed / not_fetched): no value could be computed."""
    if reason not in RUNNER_REASONS:
        raise ValueError(f"{reason!r} is not a runner reason {RUNNER_REASONS}")
    return PrefilterResult(
        simfin_id=entry.simfin_id,
        ticker=entry.ticker,
        sector_code=entry.sector_code,
        status=EXCLUDED,
        reason=reason,
        market_cap_usd=None,
        median_dollar_volume_usd=None,
        median_window_rows=None,
        price_date=None,
    )

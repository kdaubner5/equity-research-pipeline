"""Tests for gates_layer/universe_prefilter.py (Issue #39): boundary cases
exactly on each threshold, the reason precedence, and the rule that market
cap / median dollar volume / price date are computed whatever the reason.

Thresholds are built here from round numbers and a 5-row window (not read from
config/thresholds.yaml), so a recalibration of the real config cannot change
what these tests mean; one smoke test still runs the real config."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from conftest import DEFAULT_THRESHOLDS
from config_layer.thresholds import PrefilterThresholds
from data_layer.simfin_universe import PriceRow, UniverseEntry
from gates_layer import universe_prefilter as pf

AS_OF = date(2026, 9, 24)
T = PrefilterThresholds(
    min_market_cap_usd=100_000_000.0,
    max_market_cap_usd=1_000_000_000.0,
    min_median_dollar_volume_usd=500_000.0,
    adv_window_trading_days=5,
    max_price_age_days=3,
)
ENTRY = UniverseEntry(42, "ABC", "Abc Inc", "US", 106005, "Drug Manufacturers", "Healthcare")
UNCLASSIFIED = UniverseEntry(43, "NOC", "No Class", "US", None, None, None)

# 10.0 x 1_000_000 = 10_000_000 USD per day is far above the 500_000 minimum
CLOSE, VOLUME = 10.0, 1_000_000


def rows(n=5, *, last=None, close=CLOSE, volume=VOLUME, shares=20_000_000):
    """n consecutive daily rows ending at `last` (default: the day before as_of)."""
    last = last or AS_OF - timedelta(days=1)
    return [PriceRow(last - timedelta(days=n - 1 - i), close, volume, shares) for i in range(n)]


def evaluate(price_rows, entry=ENTRY, thresholds=T):
    return pf.evaluate_prefilter(entry, price_rows, as_of=AS_OF, thresholds=thresholds)


def test_a_company_inside_every_threshold_passes_with_no_reason():
    result = evaluate(rows())
    assert (result.status, result.reason) == (pf.PASSED, None)
    assert result.market_cap_usd == 200_000_000.0
    assert result.median_dollar_volume_usd == 10_000_000.0
    assert result.median_window_rows == 5
    assert result.price_date == AS_OF - timedelta(days=1)
    assert (result.simfin_id, result.ticker, result.sector_code) == (42, "ABC", 106005)


def test_the_real_config_thresholds_run_through_the_same_function():
    # smoke test: the values are computed from the real config's window; the
    # reason itself depends on the configured thresholds, so only the
    # status/reason consistency is asserted, not a specific outcome.
    thresholds = DEFAULT_THRESHOLDS.prefilter
    result = pf.evaluate_prefilter(
        ENTRY, rows(thresholds.adv_window_trading_days, shares=25_000_000), as_of=AS_OF, thresholds=thresholds
    )
    assert result.market_cap_usd == 250_000_000.0
    assert result.median_window_rows == thresholds.adv_window_trading_days
    assert result.status == (pf.PASSED if result.reason is None else pf.EXCLUDED)


# --- exactly on each threshold ----------------------------------------------


@pytest.mark.parametrize(
    "shares,expected_reason",
    [
        (10_000_000, None),  # 10.0 x 10_000_000 = 100_000_000 = min: exactly on the threshold passes
        (9_999_999, pf.MARKET_CAP_BELOW_MIN),  # 99_999_990 < min
        (100_000_000, None),  # 1_000_000_000 = max: exactly on the threshold passes
        (100_000_001, pf.MARKET_CAP_ABOVE_MAX),  # 1_000_000_010 > max
    ],
    ids=["cap_at_min", "cap_just_below_min", "cap_at_max", "cap_just_above_max"],
)
def test_market_cap_boundaries(shares, expected_reason):
    assert evaluate(rows(shares=shares)).reason == expected_reason


@pytest.mark.parametrize(
    "volume,expected_reason",
    [
        (50_000, None),  # 10.0 x 50_000 = 500_000 = min: exactly on the threshold passes
        (49_999, pf.MEDIAN_DOLLAR_VOLUME_BELOW_MIN),  # 499_990 < min
    ],
    ids=["median_at_min", "median_just_below_min"],
)
def test_median_dollar_volume_boundary(volume, expected_reason):
    assert evaluate(rows(volume=volume)).reason == expected_reason


@pytest.mark.parametrize(
    "age_days,expected_reason",
    [(3, None), (4, pf.PRICE_STALE), (0, None)],
    ids=["age_at_max", "age_just_over_max", "same_day"],
)
def test_price_age_boundary(age_days, expected_reason):
    result = evaluate(rows(last=AS_OF - timedelta(days=age_days)))
    assert result.reason == expected_reason


@pytest.mark.parametrize(
    "n_rows,expected_reason",
    [(5, None), (4, pf.INSUFFICIENT_PRICE_HISTORY), (1, pf.INSUFFICIENT_PRICE_HISTORY)],
    ids=["exactly_window", "window_minus_one", "single_row"],
)
def test_history_boundary_is_the_configured_window(n_rows, expected_reason):
    assert evaluate(rows(n_rows)).reason == expected_reason


# --- median mechanics ---------------------------------------------------------


def test_median_of_an_even_window_is_the_mean_of_the_two_middle_values():
    t = replace(T, adv_window_trading_days=4)
    volumes = [40_000, 100_000, 200_000, 1_000_000]  # x 10.0 -> 400k, 1M, 2M, 10M; median (1M + 2M) / 2
    price_rows = [PriceRow(AS_OF - timedelta(days=4 - i), 10.0, v, 20_000_000) for i, v in enumerate(volumes)]
    assert evaluate(price_rows, thresholds=t).median_dollar_volume_usd == 1_500_000.0


def test_median_of_an_odd_window_is_the_middle_value():
    volumes = [10_000, 20_000, 90_000, 100_000, 5_000_000]  # x 10.0 -> median 900_000
    price_rows = [PriceRow(AS_OF - timedelta(days=5 - i), 10.0, v, 20_000_000) for i, v in enumerate(volumes)]
    assert evaluate(price_rows).median_dollar_volume_usd == 900_000.0


def test_only_the_last_window_rows_enter_the_median_older_rows_are_ignored():
    old = [PriceRow(AS_OF - timedelta(days=20 + i), 10.0, 999_999_999, 20_000_000) for i in range(5)]
    result = evaluate(old + rows(5, volume=100_000))
    assert result.median_dollar_volume_usd == 1_000_000.0 and result.median_window_rows == 5


def test_zero_volume_is_a_real_trading_day_and_counts_toward_the_median():
    volumes = [0, 0, 0, 0, 5_000_000]  # median 0
    price_rows = [PriceRow(AS_OF - timedelta(days=5 - i), 10.0, v, 20_000_000) for i, v in enumerate(volumes)]
    result = evaluate(price_rows)
    assert result.median_dollar_volume_usd == 0.0 and result.median_window_rows == 5
    assert result.reason == pf.MEDIAN_DOLLAR_VOLUME_BELOW_MIN


def test_rows_without_volume_or_with_a_non_positive_close_are_skipped_not_counted():
    good = rows(5)
    bad = [
        PriceRow(AS_OF - timedelta(days=30), 10.0, None, 20_000_000),  # no volume
        PriceRow(AS_OF - timedelta(days=31), 0.0, 1_000_000, 20_000_000),  # close 0
        PriceRow(AS_OF - timedelta(days=32), -3.0, 1_000_000, 20_000_000),  # negative close
    ]
    result = evaluate(bad + good)
    assert result.median_window_rows == 5 and result.reason is None


# A row with close 0 (SLNM type: SimFin keeps writing rows with volume but a price of 0) is never a price -
# also when it is the LAST row of the window, not only when it lies outside it.


def test_rows_that_all_have_close_zero_are_no_price_data_with_every_value_missing():
    result = evaluate(rows(5, close=0.0))
    assert (result.status, result.reason) == (pf.EXCLUDED, pf.NO_PRICE_DATA)
    assert (result.market_cap_usd, result.price_date, result.median_dollar_volume_usd, result.median_window_rows) == (None, None, None, None)


@pytest.mark.parametrize("n_zero", [1, 3], ids=["last_row", "last_three_rows"])
def test_a_last_row_with_close_zero_after_stale_valid_rows_is_price_stale_priced_at_the_last_valid_row(n_zero):
    stale_end = AS_OF - timedelta(days=10)  # limit 3 days
    result = evaluate(rows(5, last=stale_end) + rows(n_zero, close=0.0))  # then rows up to yesterday with close 0
    assert (result.status, result.reason) == (pf.EXCLUDED, pf.PRICE_STALE)
    assert result.price_date == stale_end  # not the zero row
    assert result.market_cap_usd == 10.0 * 20_000_000  # valued at the last valid price, not at 0
    assert (result.median_dollar_volume_usd, result.median_window_rows) == (10_000_000.0, 5)  # the zero rows are not in the window


def test_a_zero_last_row_after_a_recent_valid_row_takes_price_date_and_market_cap_from_the_valid_row():
    # the price age counts from the last row WITH a price (docs/decisions.md, Entscheidung 3); the status is not asserted here
    result = evaluate(rows(5, last=AS_OF - timedelta(days=2)) + rows(1, close=0.0))
    assert result.price_date == AS_OF - timedelta(days=2)
    assert result.market_cap_usd == 10.0 * 20_000_000
    assert (result.median_dollar_volume_usd, result.median_window_rows) == (10_000_000.0, 5)


def test_input_order_does_not_matter_and_the_price_date_is_the_latest_priced_row():
    shuffled = list(reversed(rows(5)))
    result = evaluate(shuffled)
    assert result.price_date == AS_OF - timedelta(days=1)
    unpriced_tail = rows(5) + [PriceRow(AS_OF, None, None, None)]  # today's row has no price yet
    assert evaluate(unpriced_tail).price_date == AS_OF - timedelta(days=1)


def test_a_row_dated_after_as_of_is_a_caller_bug_not_company_data():
    with pytest.raises(ValueError, match="after as_of"):
        evaluate(rows(5) + [PriceRow(AS_OF + timedelta(days=1), 10.0, 1, 1)])


# --- precedence -----------------------------------------------------------------


def test_no_classification_wins_over_everything_but_the_values_are_still_computed():
    result = evaluate(rows(5, last=AS_OF - timedelta(days=30), shares=1), entry=UNCLASSIFIED)  # also stale, tiny cap
    assert result.reason == pf.NO_CLASSIFICATION
    assert result.market_cap_usd == 10.0 and result.price_date == AS_OF - timedelta(days=30)
    assert result.median_dollar_volume_usd == 10_000_000.0


@pytest.mark.parametrize("price_rows", [None, [], [PriceRow(AS_OF - timedelta(days=1), None, 1000, 5)]], ids=["none", "empty", "no_close"])
def test_no_priced_row_is_no_price_data_with_every_value_missing(price_rows):
    result = evaluate(price_rows)
    assert result.reason == pf.NO_PRICE_DATA
    assert (result.market_cap_usd, result.price_date) == (None, None)


def test_a_priced_row_without_a_volume_is_price_data_but_has_no_median():
    result = evaluate([PriceRow(AS_OF - timedelta(days=1), 10.0, None, 20_000_000)])
    assert result.price_date == AS_OF - timedelta(days=1) and result.market_cap_usd == 200_000_000.0
    assert result.median_dollar_volume_usd is None and result.median_window_rows is None
    assert result.reason == pf.INSUFFICIENT_PRICE_HISTORY


def test_stale_wins_over_size_and_liquidity_and_still_reports_them():
    result = evaluate(rows(5, last=AS_OF - timedelta(days=10), shares=1, volume=1))
    assert result.reason == pf.PRICE_STALE
    assert result.market_cap_usd == 10.0 and result.median_dollar_volume_usd == 10.0
    assert result.price_date == AS_OF - timedelta(days=10)


@pytest.mark.parametrize("shares", [None, 0, -5], ids=["null", "zero", "negative"])
def test_missing_or_non_positive_shares_is_no_shares_without_a_market_cap(shares):
    result = evaluate(rows(5, shares=shares))
    assert result.reason == pf.NO_SHARES
    assert result.market_cap_usd is None
    assert result.median_dollar_volume_usd == 10_000_000.0 and result.price_date == AS_OF - timedelta(days=1)


def test_no_shares_wins_over_a_thin_history():
    assert evaluate(rows(2, shares=None)).reason == pf.NO_SHARES


def test_market_cap_below_min_wins_over_insufficient_history_and_low_volume():
    result = evaluate(rows(2, shares=1_000, volume=1))
    assert result.reason == pf.MARKET_CAP_BELOW_MIN


def test_market_cap_above_max_wins_over_insufficient_history_and_low_volume():
    result = evaluate(rows(2, shares=1_000_000_000, volume=1))
    assert result.reason == pf.MARKET_CAP_ABOVE_MAX


def test_insufficient_history_wins_over_low_volume_and_is_excluded_even_when_the_median_would_pass():
    low_volume = evaluate(rows(3, volume=1))
    assert low_volume.reason == pf.INSUFFICIENT_PRICE_HISTORY and low_volume.median_window_rows == 3
    assert low_volume.median_dollar_volume_usd == 10.0  # computed over the rows that exist
    assert evaluate(rows(3)).reason == pf.INSUFFICIENT_PRICE_HISTORY


@pytest.mark.parametrize(
    "price_rows,entry,reason",
    [
        (rows(5), ENTRY, None),
        (rows(5), UNCLASSIFIED, pf.NO_CLASSIFICATION),
        (None, ENTRY, pf.NO_PRICE_DATA),
        (rows(5, last=AS_OF - timedelta(days=9)), ENTRY, pf.PRICE_STALE),
        (rows(5, shares=None), ENTRY, pf.NO_SHARES),
        (rows(5, shares=1_000), ENTRY, pf.MARKET_CAP_BELOW_MIN),
        (rows(5, shares=500_000_000), ENTRY, pf.MARKET_CAP_ABOVE_MAX),
        (rows(2), ENTRY, pf.INSUFFICIENT_PRICE_HISTORY),
        (rows(5, volume=1), ENTRY, pf.MEDIAN_DOLLAR_VOLUME_BELOW_MIN),
    ],
    ids=["passed", "no_classification", "no_price_data", "price_stale", "no_shares", "below_min", "above_max", "history", "median"],
)
def test_status_is_passed_iff_there_is_no_reason(price_rows, entry, reason):
    result = evaluate(price_rows, entry=entry)
    assert result.reason == reason
    assert result.status == (pf.PASSED if reason is None else pf.EXCLUDED)


def test_a_value_is_reported_whenever_its_data_exists_whatever_the_reason():
    # every reason that follows a computable value keeps that value
    for price_rows, entry in [
        (rows(5, shares=1_000), ENTRY),  # below_min
        (rows(5, shares=500_000_000), ENTRY),  # above_max
        (rows(2), ENTRY),  # history
        (rows(5, volume=1), ENTRY),  # median
        (rows(5, last=AS_OF - timedelta(days=9)), ENTRY),  # stale
    ]:
        result = evaluate(price_rows, entry=entry)
        assert result.market_cap_usd is not None
        assert result.median_dollar_volume_usd is not None
        assert result.price_date is not None


# --- runner reasons -----------------------------------------------------------------


@pytest.mark.parametrize("reason", [pf.PRICE_FETCH_FAILED, pf.NOT_FETCHED])
def test_runner_excluded_carries_identity_and_no_values(reason):
    result = pf.runner_excluded(ENTRY, reason)
    assert (result.status, result.reason) == (pf.EXCLUDED, reason)
    assert (result.simfin_id, result.ticker, result.sector_code) == (42, "ABC", 106005)
    assert (result.market_cap_usd, result.median_dollar_volume_usd, result.median_window_rows, result.price_date) == (
        None,
        None,
        None,
        None,
    )


def test_runner_excluded_refuses_a_decided_reason():
    with pytest.raises(ValueError, match="runner reason"):
        pf.runner_excluded(ENTRY, pf.PRICE_STALE)


def test_the_reason_vocabulary_is_exactly_the_documented_one():
    assert pf.PREFILTER_REASONS == (
        "no_classification",
        "delisted_marker",
        "no_price_data",
        "price_stale",
        "no_shares",
        "market_cap_below_min",
        "market_cap_above_max",
        "insufficient_price_history",
        "median_dollar_volume_below_min",
        "price_fetch_failed",
        "not_fetched",
    )


# --- delisted_marker ---------------------------------------------------------------


def entry_with_ticker(ticker, *, classified=True):
    sector = (106005, "Drug Manufacturers", "Healthcare") if classified else (None, None, None)
    return UniverseEntry(77, ticker, "Some Co", "US", sector[0], sector[1], sector[2])


def test_the_suffix_list_is_the_two_forms_found_in_the_listing():
    assert pf.DELISTED_TICKER_SUFFIXES == ("_delisted", "_old")


@pytest.mark.parametrize("ticker", ["AVGO_old", "EE_delisted", "abc_OLD", "Abc_Delisted", "BRK-B_old"])
def test_a_ticker_with_a_delisted_or_old_suffix_has_the_marker(ticker):
    assert pf.has_delisted_marker(ticker)
    assert evaluate(rows(), entry=entry_with_ticker(ticker)).reason == pf.DELISTED_MARKER


@pytest.mark.parametrize("ticker", ["OLD", "GOLD", "A_OLDER", "BRK-B", "ABC.A", "_old_x", "", None])
def test_only_a_real_suffix_counts_not_a_similar_substring(ticker):
    assert not pf.has_delisted_marker(ticker)


def test_the_marker_comes_right_after_no_classification_and_before_every_other_reason():
    # would otherwise be: stale, tiny cap, thin history
    stale_tiny = rows(2, last=AS_OF - timedelta(days=30), shares=1, volume=1)
    marked = entry_with_ticker("DEAD_delisted")
    assert evaluate(stale_tiny, entry=marked).reason == pf.DELISTED_MARKER
    assert evaluate(None, entry=marked).reason == pf.DELISTED_MARKER  # even with no price data at all
    # classification is checked first
    assert evaluate(rows(), entry=entry_with_ticker("DEAD_old", classified=False)).reason == pf.NO_CLASSIFICATION


def test_a_marked_company_passed_prices_still_reports_its_values_a_marked_one_without_prices_has_none():
    with_rows = evaluate(rows(), entry=entry_with_ticker("X_old"))
    assert with_rows.status == pf.EXCLUDED and with_rows.market_cap_usd == 200_000_000.0
    without = evaluate(None, entry=entry_with_ticker("X_old"))
    assert (without.market_cap_usd, without.median_dollar_volume_usd, without.median_window_rows, without.price_date) == (
        None,
        None,
        None,
        None,
    )


def test_unknown_underscore_tickers_are_listed_but_not_treated_as_marked():
    tickers = ["A_x", "B_old", "C", None, "D_delisted", "E_merged", "F_OLD"]
    assert pf.unknown_underscore_tickers(tickers) == ["A_x", "E_merged"]
    assert evaluate(rows(), entry=entry_with_ticker("E_merged")).reason is None  # a new marker form is not excluded

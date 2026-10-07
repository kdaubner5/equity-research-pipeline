"""Trend-Guard ratios (docs/decisions.md "Trend-Guard", Abschnitte 2 und 8, Test 1): window, revenue,
EBITDA-margin and last-year ROIC. Ratios are exact Fractions so a value exactly on a threshold is exact."""

from fractions import Fraction

import pytest

from metrics_layer.result import MetricInputError
from metrics_layer.trend import ebitda_margin_ratio, revenue_ratio, roic_last_year, trend_window
from trend_helpers import ROIC, make_config, period, periods_from

AS_OF_YEAR = 2026


def window_of(series, **fixed):
    return periods_from(series, **fixed)


# ------------------------------------------------------------------ window


def test_window_is_the_years_most_recent_periods_in_ascending_order():
    periods = periods_from([{}] * 5, last_year=2025)
    window, reason = trend_window(list(reversed(periods)), as_of_year=AS_OF_YEAR, max_fiscal_year_lag=1, config=make_config(years=3))
    assert reason is None
    assert [p["fiscal_year"] for p in window] == [2023, 2024, 2025]


def test_window_with_exactly_min_years_is_valid():
    window, reason = trend_window(periods_from([{}] * 3), as_of_year=AS_OF_YEAR, max_fiscal_year_lag=1, config=make_config())
    assert reason is None and len(window) == 3


def test_fewer_periods_than_min_years_is_insufficient_never_averaged_over_fewer():
    window, reason = trend_window(periods_from([{}] * 2), as_of_year=AS_OF_YEAR, max_fiscal_year_lag=1, config=make_config())
    assert window is None and reason == "trend_insufficient_years"


def test_no_periods():
    assert trend_window([], as_of_year=AS_OF_YEAR, max_fiscal_year_lag=1, config=make_config()) == (None, "no_periods")


def test_stale_latest_year_uses_the_stage2_lag():
    fresh = periods_from([{}] * 3, last_year=2025)  # as_of 2026, lag 1: 2025 is fine
    assert trend_window(fresh, as_of_year=AS_OF_YEAR, max_fiscal_year_lag=1, config=make_config())[1] is None
    older = periods_from([{}] * 3, last_year=2024)  # 2024 < 2026 - 1
    assert trend_window(older, as_of_year=AS_OF_YEAR, max_fiscal_year_lag=1, config=make_config()) == (None, "trend_fundamentals_stale")
    assert trend_window(older, as_of_year=AS_OF_YEAR, max_fiscal_year_lag=2, config=make_config())[1] is None


def test_stale_is_checked_before_insufficient_years():
    assert trend_window(periods_from([{}] * 2, last_year=2020), as_of_year=AS_OF_YEAR, max_fiscal_year_lag=1, config=make_config())[1] == "trend_fundamentals_stale"


def test_duplicate_fiscal_years_are_a_caller_error():
    periods = periods_from([{}] * 3)
    periods.append(dict(periods[0]))
    with pytest.raises(MetricInputError):
        trend_window(periods, as_of_year=AS_OF_YEAR, max_fiscal_year_lag=1, config=make_config())


def test_a_gap_in_the_window_is_not_filled_or_skipped():
    periods = [period(2021), period(2023), period(2025)]  # the mapper may drop a year; no consecutive years are required
    window, reason = trend_window(periods, as_of_year=AS_OF_YEAR, max_fiscal_year_lag=1, config=make_config())
    assert reason is None and [p["fiscal_year"] for p in window] == [2021, 2023, 2025]


# ------------------------------------------------------------------ revenue


def test_revenue_hand_example_110_100_90_is_exactly_0_9():
    result = revenue_ratio(window_of([{"revenue": 110}, {"revenue": 100}, {"revenue": 90}]))
    assert result.invalid_reason is None and result.value == Fraction(9, 10)


def test_revenue_hand_example_110_100_89():
    result = revenue_ratio(window_of([{"revenue": 110}, {"revenue": 100}, {"revenue": 89}]))
    assert result.value == Fraction(89 * 3, 299)
    assert float(result.value) == pytest.approx(0.893, abs=1e-3)


def test_revenue_uses_the_last_year_against_the_mean_of_the_whole_window():
    result = revenue_ratio(window_of([{"revenue": 100}, {"revenue": 100}, {"revenue": 100}, {"revenue": 130}]))
    assert result.value == Fraction(130 * 4, 430)


@pytest.mark.parametrize("bad_year", [0, 1, 2])
@pytest.mark.parametrize("value", [0, -5.0])
def test_revenue_not_positive_in_any_year_is_not_computable(bad_year, value):
    series = [{"revenue": 100}] * 3
    series[bad_year] = {"revenue": value}
    assert revenue_ratio(window_of(series)).invalid_reason == "revenue_non_positive"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 10**400, "100", True, None])
def test_revenue_non_finite_or_wrong_type_is_never_unremarkable(value):
    series = [{"revenue": 100}, {"revenue": value}, {"revenue": 100}]
    result = revenue_ratio(window_of(series))
    assert result.value is None and result.invalid_reason == "non_finite_input"


# ------------------------------------------------------------------ EBITDA margin


def test_margin_hand_example_7_7_10_on_revenue_100_is_exactly_1_25():
    result = ebitda_margin_ratio(window_of([{"ebitda": 7}, {"ebitda": 7}, {"ebitda": 10}], revenue=100))
    assert result.invalid_reason is None and result.value == Fraction(5, 4)


def test_margin_hand_example_7_7_11_is_1_32():
    result = ebitda_margin_ratio(window_of([{"ebitda": 7}, {"ebitda": 7}, {"ebitda": 11}], revenue=100))
    assert float(result.value) == pytest.approx(1.32, abs=1e-9)


def test_margin_is_the_mean_of_the_yearly_margins_not_of_the_totals():
    series = [{"revenue": 100, "ebitda": 10}, {"revenue": 1000, "ebitda": 200}, {"revenue": 100, "ebitda": 10}]
    result = ebitda_margin_ratio(window_of(series))
    margins = [Fraction(1, 10), Fraction(1, 5), Fraction(1, 10)]
    assert result.value == margins[-1] / (sum(margins) / 3)


def test_margin_float_inputs_are_handled_exactly_from_their_decimal_text_free_value():
    # 0.1 / 0.08 style cases must not be decided by float rounding: ints stay exact
    result = ebitda_margin_ratio(window_of([{"ebitda": 90}, {"ebitda": 90}, {"ebitda": 90}], revenue=1000))
    assert result.value == 1


def test_margin_mean_not_positive_is_not_computable():
    series = [{"ebitda": 10}, {"ebitda": -20}, {"ebitda": 10}]  # mean margin is 0
    assert ebitda_margin_ratio(window_of(series, revenue=100)).invalid_reason == "margin_mean_non_positive"
    series = [{"ebitda": -10}, {"ebitda": -20}, {"ebitda": -10}]
    assert ebitda_margin_ratio(window_of(series, revenue=100)).invalid_reason == "margin_mean_non_positive"


def test_margin_with_a_negative_last_year_but_positive_mean_is_computable_and_negative():
    series = [{"ebitda": 20}, {"ebitda": 15}, {"ebitda": -5}]
    result = ebitda_margin_ratio(window_of(series, revenue=100))
    assert result.invalid_reason is None and result.value < 0


def test_margin_revenue_not_positive_is_checked_before_the_mean():
    assert ebitda_margin_ratio(window_of([{"revenue": 100}, {"revenue": 100}, {"revenue": 0}])).invalid_reason == "revenue_non_positive"


@pytest.mark.parametrize("field", ["revenue", "ebitda"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), 10**400, "5", None])
def test_margin_non_finite_or_wrong_type_is_never_unremarkable(field, value):
    series = [{}, {field: value}, {}]
    assert ebitda_margin_ratio(window_of(series)).invalid_reason == "non_finite_input"


# ------------------------------------------------------------------ ROIC of the last year


def good_year(**overrides):
    return {"ebit": 20.0, "equity": 150.0, "net_debt": 50.0, "total_assets": 300.0, **overrides}


def test_roic_last_year_is_the_existing_single_year_roic():
    # NOPAT 20 * 0.75 = 15, invested capital 200 -> 7.5 %
    result = roic_last_year(window_of([good_year(), good_year(), good_year()]), thresholds=ROIC)
    assert result.invalid_reason is None and result.value == pytest.approx(0.075)


def test_roic_last_year_ignores_earlier_years_even_if_they_are_invalid():
    series = [good_year(net_debt=None), good_year(equity=-1000), good_year()]
    assert roic_last_year(window_of(series), thresholds=ROIC).invalid_reason is None


def test_roic_unreliable_net_debt_in_the_last_year():
    series = [good_year(), good_year(), good_year(net_debt=None)]
    assert roic_last_year(window_of(series), thresholds=ROIC).invalid_reason == "net_debt_unreliable"


def test_roic_completely_missing_debt_data_keeps_its_own_reason():
    series = [good_year(), good_year(), good_year(net_debt=None, flags=("debt_data_completely_missing",))]
    assert roic_last_year(window_of(series), thresholds=ROIC).invalid_reason == "debt_data_completely_missing"


@pytest.mark.parametrize("override,reason", [
    ({"equity": -300.0}, "ic_non_positive"),
    ({"total_assets": 0}, "total_assets_unreliable"),
    ({"ebit": 400.0}, "roic_implausibly_high"),
    ({"total_assets": 5000.0}, "ic_below_asset_floor"),
])
def test_roic_existing_guards_apply_to_the_last_year(override, reason):
    series = [good_year(), good_year(), good_year(**override)]
    assert roic_last_year(window_of(series), thresholds=ROIC).invalid_reason == reason


@pytest.mark.parametrize("field", ["ebit", "equity", "net_debt", "total_assets"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), 10**400, "5"])
def test_roic_non_finite_or_wrong_type_in_the_last_year_is_not_computable(field, value):
    series = [good_year(), good_year(), good_year(**{field: value})]
    assert roic_last_year(window_of(series), thresholds=ROIC).invalid_reason == "non_finite_input"

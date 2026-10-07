"""Share-count ratio and the other-investing counter-calculation (docs/decisions.md "Stufe 2 -
Datenplausibilitaet: Aktienzahl und Investitionen", Abschnitte 1, 2 und 5, Test 1).
Hand-computed expectations, artificial periods."""

import pytest

from metrics_layer.plausibility import (
    SHARE_AMBIGUOUS,
    SHARE_OK,
    SHARE_SUSPECT,
    classify_share_count_ratio,
    share_count_ratio,
)
from metrics_layer.result import MetricInputError
from metrics_layer.valuation import fcf_yield_incl_other_investing, other_investing_triggered
from stage2_helpers import capex_for, make_config, period, three_periods

CONFIG = make_config()
AS_OF_YEAR = 2026
NAN = float("nan")
INF = float("inf")
HUGE = 10**400


# ---------------------------------------------------------------- share-count ratio R


def test_ratio_hand_example_matches_the_payment_rhythm():
    # 100 Mio paid, 25 Mio shares, 1.00 per share and payment -> 4 payments a year
    result = share_count_ratio(-100.0, 25.0, 1.0)
    assert result.invalid_reason is None
    assert result.value == pytest.approx(4.0)


def test_ratio_explodes_when_the_share_count_is_too_small():
    assert share_count_ratio(-100.0, 5.0, 1.0).value == pytest.approx(20.0)


def test_dividend_paid_is_read_from_its_cash_flow_sign_an_outflow_is_negative():
    assert share_count_ratio(-100.0, 25.0, 1.0).invalid_reason is None
    assert share_count_ratio(100.0, 25.0, 1.0).invalid_reason == "dividend_not_positive"  # an inflow is not a payment


@pytest.mark.parametrize(
    "paid,shares,dps,reason",
    [
        (None, 25.0, 1.0, "dividend_data_missing"),
        (-100.0, None, 1.0, "dividend_data_missing"),
        (-100.0, 25.0, None, "dividend_data_missing"),
        (0, 25.0, 1.0, "dividend_not_positive"),
        (-100.0, 25.0, 0, "dividend_not_positive"),
        (-100.0, 25.0, -1.0, "dividend_not_positive"),
        (-100.0, 0, 1.0, "shares_not_positive"),
        (-100.0, -5.0, 1.0, "shares_not_positive"),
    ],
)
def test_ratio_is_not_checkable_without_usable_inputs(paid, shares, dps, reason):
    result = share_count_ratio(paid, shares, dps)
    assert result.value is None and result.invalid_reason == reason


@pytest.mark.parametrize("bad", [NAN, INF, -INF, HUGE, -HUGE])
def test_ratio_never_treats_non_finite_inputs_as_a_valid_value(bad):
    assert share_count_ratio(bad, 25.0, 1.0).invalid_reason in ("non_finite_input", "dividend_not_positive")
    assert share_count_ratio(-100.0, bad, 1.0).invalid_reason in ("non_finite_input", "shares_not_positive")
    assert share_count_ratio(-100.0, 25.0, bad).invalid_reason in ("non_finite_input", "dividend_not_positive")


def test_ratio_that_overflows_is_not_valid():
    assert share_count_ratio(-1e308, 1e-300, 1e-300).invalid_reason == "non_finite_input"


@pytest.mark.parametrize("bad", ["x", [1], True, {}])
def test_ratio_rejects_a_wrong_type_instead_of_crashing(bad):
    assert share_count_ratio(bad, 25.0, 1.0).invalid_reason == "non_finite_input"
    assert share_count_ratio(-100.0, bad, 1.0).invalid_reason == "non_finite_input"
    assert share_count_ratio(-100.0, 25.0, bad).invalid_reason == "non_finite_input"


# ---------------------------------------------------------------- classification of R


@pytest.mark.parametrize(
    "ratio,verdict",
    [
        (0.1, SHARE_OK),  # a share count that is too LARGE only makes a company look dear: no check on that side
        (3.92, SHARE_OK),
        (6.0, SHARE_OK),  # exactly on the threshold: not suspect (strict '>')
        (6.0001, SHARE_SUSPECT),
        (8.99, SHARE_SUSPECT),
        (9.0, SHARE_AMBIGUOUS),  # monthly band, both edges inclusive
        (12.0, SHARE_AMBIGUOUS),
        (15.0, SHARE_AMBIGUOUS),
        (15.01, SHARE_SUSPECT),
        (347.0, SHARE_SUSPECT),
    ],
)
def test_classification_boundaries(ratio, verdict):
    assert classify_share_count_ratio(ratio, CONFIG) == verdict


def test_classification_follows_the_config():
    config = make_config(share_count_check={"suspect_ratio_above": 5.0, "monthly_band_low": 7.0, "monthly_band_high": 8.0})
    assert classify_share_count_ratio(5.5, config) == SHARE_SUSPECT
    assert classify_share_count_ratio(7.5, config) == SHARE_AMBIGUOUS
    assert classify_share_count_ratio(5.0, config) == SHARE_OK


# ---------------------------------------------------------------- other investing: trigger


def _trigger(periods, capex, other, config=CONFIG):
    return other_investing_triggered(periods, capex, other, config=config)


def test_trigger_needs_more_than_the_configured_multiple_in_one_year():
    periods = three_periods()
    capex = capex_for(periods, -30.0)
    assert _trigger(periods, capex, {2023: -60.0, 2024: -60.0, 2025: -60.0}) is False  # exactly 2.0: not 'more than'
    assert _trigger(periods, capex, {2023: -60.01, 2024: 0.0, 2025: 0.0}) is True  # one year is enough


def test_trigger_uses_magnitudes_inflows_trigger_too_but_the_counter_calculation_then_cannot_hurt():
    periods = three_periods()
    assert _trigger(periods, capex_for(periods, -30.0), {2025: 500.0, 2024: 0.0, 2023: 0.0}) is True


def test_trigger_with_zero_capex_and_any_other_investing():
    periods = three_periods()
    assert _trigger(periods, capex_for(periods, 0.0), {2025: -5.0, 2024: 0.0, 2023: 0.0}) is True
    assert _trigger(periods, capex_for(periods, 0.0), {2025: 0.0, 2024: 0.0, 2023: 0.0}) is False


def test_trigger_skips_years_without_other_investing_and_is_unchecked_without_any():
    periods = three_periods()
    capex = capex_for(periods, -30.0)
    assert _trigger(periods, capex, {2023: None, 2024: -200.0, 2025: None}) is True
    assert _trigger(periods, capex, {2023: None, 2024: None, 2025: None}) is None
    assert _trigger(periods, capex, {}) is None


def test_trigger_skips_years_without_a_capex_value():
    periods = three_periods()
    assert _trigger(periods, {2023: None, 2024: None, 2025: None}, {2024: -200.0}) is None
    assert _trigger(periods, {2023: None, 2024: -30.0, 2025: None}, {2023: -999.0, 2024: -10.0}) is False


def test_trigger_ignores_years_outside_the_fcf_window():
    periods = [period(2022)] + three_periods()
    capex = {2022: -1.0, **capex_for(three_periods(), -30.0)}
    assert _trigger(periods, capex, {2022: -999.0, 2023: 0.0, 2024: 0.0, 2025: 0.0}) is False


def test_trigger_ratio_comes_from_the_config():
    periods = three_periods()
    capex = capex_for(periods, -30.0)
    other = {2023: -100.0, 2024: 0.0, 2025: 0.0}
    assert _trigger(periods, capex, other) is True
    assert _trigger(periods, capex, other, make_config(other_investing_check={"min_ratio_to_capex": 4.0})) is False


@pytest.mark.parametrize("bad", [NAN, INF, -INF, HUGE, "x", True])
def test_trigger_skips_a_malformed_other_investing_value_like_a_missing_one(bad):
    periods = three_periods()
    capex = capex_for(periods, -30.0)
    assert _trigger(periods, capex, {2025: bad, 2024: 0.0, 2023: 0.0}) is False  # the two readable years decide
    assert _trigger(periods, capex, {2025: bad, 2024: None, 2023: None}) is None  # nothing readable left: unchecked


def test_trigger_duplicate_fiscal_years_are_a_caller_error():
    with pytest.raises(MetricInputError):
        _trigger([period(2025), period(2025), period(2024)], {2025: -1.0, 2024: -1.0}, {2025: -9.0})


# ---------------------------------------------------------------- other investing: counter-calculation of the FCF-yield


def _alt(periods, capex, other, market_cap=1000.0, config=CONFIG):
    return fcf_yield_incl_other_investing(periods, capex, other, market_cap, as_of_year=AS_OF_YEAR, config=config)


def test_counter_calculation_hand_example():
    periods = three_periods(cfo=100.0)
    capex = capex_for(periods, -30.0)
    other = {2023: -200.0, 2024: -200.0, 2025: -200.0}
    # outflow 30 + 200 = 230 per year -> FCF -130 -> yield -0.13 (the main value would be 0.07)
    assert _alt(periods, capex, other).value == pytest.approx(-0.13)


def test_counter_calculation_never_nets_an_inflow_of_other_investing_against_the_capex():
    periods = three_periods(cfo=100.0)
    capex = capex_for(periods, -30.0)
    # the capex outflow of 30 stays; +500 of Other Investing is not cash and does not reduce it -> FCF 70 as in the main value
    assert _alt(periods, capex, {2023: 500.0, 2024: 500.0, 2025: 500.0}).value == pytest.approx(0.07)


def test_counter_calculation_adds_only_the_outflow_part_of_a_mixed_series():
    periods = three_periods(cfo=100.0)
    capex = capex_for(periods, -30.0)
    # outflows 30 + 0 (the inflow is ignored), 30 + 10, 30 + 200 -> FCF 70, 60, -130 -> mean (70 + 60 - 130)/3 = 0
    assert _alt(periods, capex, {2023: 40.0, 2024: -10.0, 2025: -200.0}).value == pytest.approx(0.0)


def test_counter_calculation_treats_a_missing_year_as_zero_other_investing():
    periods = three_periods(cfo=100.0)
    capex = capex_for(periods, -30.0)
    value = _alt(periods, capex, {2023: None, 2024: -200.0, 2025: -200.0}).value
    assert value == pytest.approx((70 + -130 + -130) / 3 / 1000)


def test_counter_calculation_is_never_above_the_main_value():
    from metrics_layer.valuation import fcf_yield_average

    periods = three_periods(cfo=100.0)
    capex = capex_for(periods, -30.0)
    for other in ({2025: 40.0}, {2025: -40.0}, {2025: -400.0}, {}):
        main = fcf_yield_average(periods, capex, 1000.0, as_of_year=AS_OF_YEAR, config=CONFIG).value
        assert _alt(periods, capex, other).value <= main + 1e-12


def test_counter_calculation_inherits_the_invalid_reasons_of_the_main_yield():
    periods = three_periods()
    other = {2025: -1.0}
    assert _alt(periods, {**capex_for(periods), 2024: None}, other).invalid_reason == "capex_missing"
    assert _alt(three_periods(last_year=2024), capex_for(three_periods(last_year=2024)), other).invalid_reason == "fundamentals_stale"
    assert _alt(periods, capex_for(periods), other, market_cap=None).invalid_reason == "market_cap_missing"
    assert _alt([], {}, other).invalid_reason == "no_periods"
    assert _alt(periods[1:], capex_for(periods), other).invalid_reason == "insufficient_years"


@pytest.mark.parametrize("bad", [NAN, INF, -INF, HUGE, -HUGE])
def test_counter_calculation_rejects_non_finite_other_investing(bad):
    periods = three_periods()
    assert _alt(periods, capex_for(periods), {2024: bad}).invalid_reason == "non_finite_input"


def test_counter_calculation_rejects_a_wrong_type_for_other_investing():
    periods = three_periods()
    assert _alt(periods, capex_for(periods), {2024: "x"}).invalid_reason == "non_finite_input"

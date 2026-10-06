"""EV/EBITDA, FCF-Yield and normalized EV/EBITDA (docs/decisions.md "Stufe 2: Peer-Gruppen
und relative Bewertung", Abschnitte 1, 3 und 6, Test 1). Hand-computed expectations,
artificial periods."""

import pytest

from metrics_layer.result import MetricInputError
from metrics_layer.valuation import (
    ev_ebitda_latest,
    ev_ebitda_normalized,
    fcf_yield_average,
    net_inflow_fiscal_years,
)
from stage2_helpers import capex_for, make_config, period, three_periods

CONFIG = make_config()
AS_OF_YEAR = 2026


def _ev(periods, market_cap=800.0, config=CONFIG):
    return ev_ebitda_latest(periods, market_cap, as_of_year=AS_OF_YEAR, config=config)


def _fcf(periods, capex, market_cap=1000.0, config=CONFIG):
    return fcf_yield_average(periods, capex, market_cap, as_of_year=AS_OF_YEAR, config=config)


def _norm(periods, market_cap=800.0, config=CONFIG):
    return ev_ebitda_normalized(periods, market_cap, as_of_year=AS_OF_YEAR, config=config)


# ---------------------------------------------------------------- EV/EBITDA


def test_ev_ebitda_hand_example():
    # EV = 800 + 200 = 1000, EBITDA 100 -> 10.0
    result = _ev([period(2025, ebitda=100.0, net_debt=200.0)])
    assert result.invalid_reason is None
    assert result.value == pytest.approx(10.0)


def test_ev_ebitda_uses_only_the_latest_year():
    periods = [period(2023, ebitda=10.0, net_debt=0.0), period(2025, ebitda=100.0, net_debt=200.0)]
    assert _ev(periods).value == pytest.approx(10.0)


def test_ev_ebitda_net_cash_lowers_the_enterprise_value():
    # EV = 800 - 300 = 500, EBITDA 100 -> 5.0
    assert _ev([period(2025, ebitda=100.0, net_debt=-300.0)]).value == pytest.approx(5.0)


def test_no_periods():
    assert _ev([]).invalid_reason == "no_periods"


def test_duplicate_fiscal_years_are_a_caller_error():
    with pytest.raises(MetricInputError):
        _ev([period(2025), period(2025)])


@pytest.mark.parametrize("year,reason", [(2025, None), (2024, "fundamentals_stale"), (2026, None)])
def test_staleness_is_judged_against_the_as_of_year(year, reason):
    # max_fiscal_year_lag = 1, as_of year 2026: 2025 is exactly on the limit (valid), 2024 one beyond
    assert _ev([period(year)]).invalid_reason == reason


def test_staleness_follows_the_configured_lag():
    config = make_config(fundamentals={"max_fiscal_year_lag": 2})
    assert _ev([period(2024)], config=config).invalid_reason is None
    assert _ev([period(2023)], config=config).invalid_reason == "fundamentals_stale"


@pytest.mark.parametrize("market_cap", [None, 0, -5.0])
def test_market_cap_missing_or_not_positive(market_cap):
    assert _ev([period(2025)], market_cap=market_cap).invalid_reason == "market_cap_missing"


def test_unreliable_net_debt():
    result = _ev([period(2025, net_debt=None, flags=["net_debt_unreliable"])])
    assert result.invalid_reason == "net_debt_unreliable"


def test_net_debt_missing_without_a_flag_is_unreliable():
    assert _ev([period(2025, net_debt=None)]).invalid_reason == "net_debt_unreliable"


def test_debt_data_completely_missing_is_kept_apart():
    result = _ev([period(2025, net_debt=None, flags=["debt_data_completely_missing"])])
    assert result.invalid_reason == "debt_data_completely_missing"


@pytest.mark.parametrize("ebitda", [0.0, -10.0])
def test_ebitda_not_positive(ebitda):
    assert _ev([period(2025, ebitda=ebitda)]).invalid_reason == "ebitda_non_positive"


@pytest.mark.parametrize("net_debt", [-800.0, -900.0])
def test_non_positive_enterprise_value(net_debt):
    # EV = 800 - 800 = 0 and 800 - 900 = -100: no meaningful multiple
    assert _ev([period(2025, ebitda=100.0, net_debt=net_debt)]).invalid_reason == "ev_non_positive"


def test_multiple_exactly_on_the_plausibility_cap_is_valid_and_above_is_not():
    # EV 1000 / EBITDA 10 = 100.0 on the cap; EBITDA 9.9 -> 101.01 above it
    assert _ev([period(2025, ebitda=10.0, net_debt=200.0)]).value == pytest.approx(100.0)
    assert _ev([period(2025, ebitda=9.9, net_debt=200.0)]).invalid_reason == "ev_ebitda_implausibly_high"


def test_check_order_stale_before_market_cap_before_net_debt_before_ebitda_before_ev():
    assert _ev([period(2020, net_debt=None, ebitda=-1)], market_cap=None).invalid_reason == "fundamentals_stale"
    assert _ev([period(2025, net_debt=None, ebitda=-1)], market_cap=None).invalid_reason == "market_cap_missing"
    assert _ev([period(2025, net_debt=None, ebitda=-1)]).invalid_reason == "net_debt_unreliable"
    assert _ev([period(2025, net_debt=-900.0, ebitda=-1)]).invalid_reason == "ebitda_non_positive"
    assert _ev([period(2025, net_debt=-900.0, ebitda=100)]).invalid_reason == "ev_non_positive"


# ---------------------------------------------------------------- FCF-Yield


def test_fcf_yield_hand_example():
    periods = [period(2023, cfo=100.0), period(2024, cfo=120.0), period(2025, cfo=140.0)]
    capex = {2023: -30.0, 2024: -40.0, 2025: -50.0}
    # FCF 70 / 80 / 90 -> mean 80, market cap 1000 -> 0.08
    result = _fcf(periods, capex)
    assert result.invalid_reason is None
    assert result.value == pytest.approx(0.08)


def test_fcf_yield_uses_only_the_most_recent_years():
    periods = [period(2022, cfo=1000.0)] + three_periods(cfo=100.0)
    capex = {2022: 0.0, **capex_for(three_periods(), -20.0)}
    assert _fcf(periods, capex).value == pytest.approx(0.08)  # (100 - 20) / 1000, the 2022 year is ignored


def test_net_inflow_counts_as_zero_capex_not_as_extra_cash():
    periods = three_periods(cfo=100.0)
    capex = {2023: -30.0, 2024: 50.0, 2025: -30.0}  # 2024: net cash inflow from asset sales
    # FCF 70 / 100 / 70 -> mean 80, NOT (70 + 150 + 70)/3
    assert _fcf(periods, capex).value == pytest.approx(0.08)
    assert net_inflow_fiscal_years(periods, capex, config=CONFIG) == (2024,)


def test_zero_capex_is_a_real_value_not_an_inflow():
    periods = three_periods(cfo=100.0)
    capex = capex_for(periods, 0.0)
    assert _fcf(periods, capex).value == pytest.approx(0.10)
    assert net_inflow_fiscal_years(periods, capex, config=CONFIG) == ()


def test_net_inflow_years_ignore_years_outside_the_window():
    periods = [period(2022)] + three_periods()
    capex = {2022: 99.0, **capex_for(three_periods(), -10.0)}
    assert net_inflow_fiscal_years(periods, capex, config=CONFIG) == ()


def test_negative_free_cash_flow_is_a_valid_negative_yield():
    periods = three_periods(cfo=10.0)
    result = _fcf(periods, capex_for(periods, -40.0))
    assert result.invalid_reason is None
    assert result.value == pytest.approx(-0.03)


def test_capex_none_is_missing_never_zero():
    periods = three_periods()
    capex = {**capex_for(periods), 2024: None}
    assert _fcf(periods, capex).invalid_reason == "capex_missing"


def test_capex_year_absent_from_the_map_is_missing():
    periods = three_periods()
    capex = {2023: -30.0, 2025: -30.0}
    assert _fcf(periods, capex).invalid_reason == "capex_missing"


def test_empty_capex_map_is_missing():
    assert _fcf(three_periods(), {}).invalid_reason == "capex_missing"


def test_fewer_years_than_configured():
    periods = three_periods()[1:]
    assert _fcf(periods, capex_for(periods)).invalid_reason == "insufficient_years"


def test_fewer_years_is_fine_if_the_config_asks_for_fewer():
    config = make_config(fcf_yield={"years": 1})
    periods = [period(2025, cfo=100.0)]
    assert _fcf(periods, {2025: -20.0}, config=config).value == pytest.approx(0.08)


def test_fcf_no_periods():
    assert _fcf([], {}).invalid_reason == "no_periods"


def test_fcf_duplicate_fiscal_years_are_a_caller_error():
    with pytest.raises(MetricInputError):
        _fcf([period(2025), period(2025), period(2024)], {2025: -1.0, 2024: -1.0})


def test_fcf_stale():
    periods = three_periods(last_year=2024)
    assert _fcf(periods, capex_for(periods)).invalid_reason == "fundamentals_stale"


@pytest.mark.parametrize("market_cap", [None, 0, -1.0])
def test_fcf_market_cap_missing(market_cap):
    periods = three_periods()
    assert _fcf(periods, capex_for(periods), market_cap=market_cap).invalid_reason == "market_cap_missing"


def test_yield_exactly_on_the_plausibility_cap_is_valid_and_above_is_not():
    periods = three_periods(cfo=600.0)
    assert _fcf(periods, capex_for(periods, -100.0)).value == pytest.approx(0.50)  # (600 - 100)/1000
    assert _fcf(periods, capex_for(periods, -90.0)).invalid_reason == "fcf_yield_implausibly_high"


def test_fcf_check_order_insufficient_years_before_stale_before_market_cap_before_capex():
    short_old = [period(2020)]
    assert _fcf(short_old, {}, market_cap=None).invalid_reason == "insufficient_years"
    old = three_periods(last_year=2020)
    assert _fcf(old, {}, market_cap=None).invalid_reason == "fundamentals_stale"
    assert _fcf(three_periods(), {}, market_cap=None).invalid_reason == "market_cap_missing"
    assert _fcf(three_periods(), {}).invalid_reason == "capex_missing"


# ---------------------------------------------------------------- normalized EV/EBITDA


def test_normalized_multiple_uses_the_mean_ebitda_and_the_latest_net_debt():
    periods = [
        period(2023, ebitda=60.0, net_debt=999.0),
        period(2024, ebitda=90.0, net_debt=999.0),
        period(2025, ebitda=150.0, net_debt=200.0),
    ]
    # mean EBITDA 100, EV 800 + 200 (latest net debt) = 1000 -> 10.0
    assert _norm(periods).value == pytest.approx(10.0)


def test_normalized_multiple_shows_a_peak_year():
    periods = [period(2023, ebitda=40.0), period(2024, ebitda=50.0), period(2025, ebitda=210.0)]
    plain = _ev(periods).value  # EV 1000 / 210
    normalized = _norm(periods).value  # EV 1000 / 100
    assert plain == pytest.approx(1000 / 210)
    assert normalized == pytest.approx(10.0)
    assert normalized > plain


def test_normalized_multiple_needs_enough_years():
    assert _norm([period(2024), period(2025)]).invalid_reason == "insufficient_years"


def test_normalized_multiple_mean_not_positive():
    periods = [period(2023, ebitda=-100.0), period(2024, ebitda=-50.0), period(2025, ebitda=100.0)]
    assert _norm(periods).invalid_reason == "ebitda_non_positive"


def test_normalized_multiple_inherits_the_latest_year_guards():
    periods = three_periods()
    periods[-1] = period(2025, net_debt=None)
    assert _norm(periods).invalid_reason == "net_debt_unreliable"
    assert _norm(three_periods(last_year=2024)).invalid_reason == "fundamentals_stale"
    assert _norm(three_periods(), market_cap=None).invalid_reason == "market_cap_missing"
    assert _norm([]).invalid_reason == "no_periods"


def test_normalized_multiple_has_no_plausibility_cap_of_its_own_but_non_positive_ev_is_invalid():
    periods = three_periods(ebitda=100.0, net_debt=-900.0)
    assert _norm(periods).invalid_reason == "ev_non_positive"


# ---------------------------------------------------------------- non-finite inputs (critic finding)

NAN = float("nan")
INF = float("inf")


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_ev_ebitda_rejects_non_finite_inputs(bad):
    assert _ev([period(2025)], market_cap=bad).invalid_reason == ("market_cap_missing" if bad == -INF else "non_finite_input")  # -inf is simply not positive
    assert _ev([period(2025, ebitda=bad)]).invalid_reason == "non_finite_input"
    assert _ev([period(2025, net_debt=bad)]).invalid_reason == "non_finite_input"


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_normalized_multiple_rejects_non_finite_inputs(bad):
    assert _norm(three_periods(), market_cap=bad).invalid_reason == ("market_cap_missing" if bad == -INF else "non_finite_input")
    periods = three_periods()
    periods[0] = period(2023, ebitda=bad)
    assert _norm(periods).invalid_reason == "non_finite_input"
    assert _norm([*three_periods()[:2], period(2025, net_debt=bad)]).invalid_reason == "non_finite_input"


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_fcf_yield_rejects_non_finite_inputs_and_never_reads_nan_capex_as_zero(bad):
    periods = three_periods()
    assert _fcf(periods, capex_for(periods), market_cap=bad).invalid_reason == ("market_cap_missing" if bad == -INF else "non_finite_input")
    assert _fcf([*periods[:2], period(2025, cfo=bad)], capex_for(periods)).invalid_reason == "non_finite_input"
    assert _fcf(periods, {**capex_for(periods), 2024: bad}).invalid_reason == "non_finite_input"


def test_non_finite_value_outside_the_window_is_ignored():
    periods = [period(2022, cfo=NAN)] + three_periods()
    assert _fcf(periods, {2022: NAN, **capex_for(three_periods())}).invalid_reason is None

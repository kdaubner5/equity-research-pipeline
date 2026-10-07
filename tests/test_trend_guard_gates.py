"""Trend-Guard marks and result (docs/decisions.md "Trend-Guard", Abschnitte 2, 5 und 8, Test 2): the four
signals, the three results and the pilot-fixture example table. Artificial data and the committed pilot
fixtures only."""

import pytest

from gates_layer.trend_guard import (
    MARK_MARGIN_FALLING,
    MARK_MARGIN_PEAK,
    MARK_REVENUE_FALLING,
    MARK_ROIC_BELOW_FLOOR,
    STATUS_CLEAR,
    STATUS_MARKED,
    STATUS_NOT_EVALUABLE,
    evaluate_trend,
    not_evaluable_trend_result,
)
from trend_helpers import ROIC, fixture_periods, make_config, period, periods_from, replace_roic

AS_OF_YEAR = 2025  # the pilot fixtures end in 2024; lag 1 keeps them fresh


def evaluate(periods, *, config=None, roic=ROIC, lag=1, as_of_year=AS_OF_YEAR):
    return evaluate_trend(
        periods, as_of_year=as_of_year, config=config or make_config(), roic_thresholds=roic, max_fiscal_year_lag=lag
    )


def stable(**fixed):
    return periods_from([{}, {}, {}], last_year=2025, **fixed)


def test_a_stable_company_is_clear_with_all_four_signals_computed():
    result = evaluate(stable())
    assert result.status == STATUS_CLEAR and result.marks == ()
    assert result.revenue_ratio == 1 and result.ebitda_margin_ratio == 1
    assert result.roic_last_year == pytest.approx(0.15)  # NOPAT 30 / invested capital 200
    assert (result.revenue_invalid_reason, result.margin_invalid_reason, result.roic_invalid_reason) == (None, None, None)


# ------------------------------------------------------------------ T1


def test_t1_revenue_exactly_on_the_threshold_is_not_marked_and_just_below_is():
    on_edge = periods_from([{"revenue": 110, "ebitda": 33}, {"revenue": 100, "ebitda": 30}, {"revenue": 90, "ebitda": 27}])
    assert MARK_REVENUE_FALLING not in evaluate(on_edge).marks
    below = periods_from([{"revenue": 110, "ebitda": 33}, {"revenue": 100, "ebitda": 30}, {"revenue": 89, "ebitda": 26.7}])
    assert MARK_REVENUE_FALLING in evaluate(below).marks


def test_t1_rising_revenue_is_not_marked():
    rising = periods_from([{"revenue": 90, "ebitda": 27}, {"revenue": 100, "ebitda": 30}, {"revenue": 140, "ebitda": 42}])
    assert evaluate(rising).marks == ()


# ------------------------------------------------------------------ T2 and T4


def margin_series(*ebitdas):
    return periods_from([{"ebitda": e} for e in ebitdas], revenue=100)


def test_t2_margin_exactly_on_the_lower_threshold_is_not_marked():
    # margins 0.10 0.10 x: ratio = x / ((0.2 + x)/3) = 0.9 exactly at x = 0.2*0.9/(3-0.9) = 6/70 -> use a clean series
    # 15 / 15 / 9 on revenue 100: mean 13, ratio 9/13 (marked); 11 / 10 / 9 -> mean 10, ratio exactly 0.9
    assert MARK_MARGIN_FALLING not in evaluate(margin_series(11, 10, 9)).marks
    assert MARK_MARGIN_FALLING in evaluate(margin_series(11, 10, 8.9)).marks


def test_t4_margin_exactly_on_the_upper_threshold_1_25_is_not_marked():
    result = evaluate(margin_series(7, 7, 10))
    assert result.ebitda_margin_ratio == pytest.approx(1.25)
    assert MARK_MARGIN_PEAK not in result.marks and result.status == STATUS_CLEAR


def test_t4_just_above_the_threshold_is_marked():
    result = evaluate(margin_series(7, 7, 11))
    assert result.marks == (MARK_MARGIN_PEAK,) and result.status == STATUS_MARKED


def test_t2_and_t4_exclude_each_other():
    for series in [(7, 7, 11), (11, 10, 5), (10, 10, 10), (5, 20, 5), (10, 10, 30), (30, 10, 1)]:
        marks = evaluate(margin_series(*series)).marks
        assert not ({MARK_MARGIN_FALLING, MARK_MARGIN_PEAK} <= set(marks)), series


def test_a_negative_last_margin_with_a_positive_mean_is_marked_falling():
    result = evaluate(margin_series(20, 15, -5))
    assert MARK_MARGIN_FALLING in result.marks


def test_thresholds_come_from_the_config():
    series = margin_series(11, 10, 9)  # ratio exactly 0.9
    assert MARK_MARGIN_FALLING in evaluate(series, config=make_config(ebitda_margin_ratio_flag_below=0.95)).marks
    peak = margin_series(7, 7, 10)  # ratio exactly 1.25
    assert MARK_MARGIN_PEAK in evaluate(peak, config=make_config(ebitda_margin_ratio_flag_above=1.2)).marks


# ------------------------------------------------------------------ T3


def roic_series(last_net_debt):
    # equity 150; NOPAT 15 -> ROIC = 15 / (150 + net_debt)
    return periods_from([{}, {}, {"net_debt": last_net_debt}], ebit=20.0)


def test_t3_marks_a_last_year_roic_below_the_stage1_fail_threshold():
    result = evaluate(roic_series(1000.0), roic=replace_roic(ic_asset_floor_ratio=0.0))  # 15/1150 = 1.3 %
    assert MARK_ROIC_BELOW_FLOOR in result.marks


def test_t3_a_last_year_roic_exactly_on_fail_below_is_not_marked():
    # NOPAT 15, invested capital 150 -> exactly 0.1
    result = evaluate(roic_series(0.0))
    assert result.roic_last_year == pytest.approx(0.1)
    assert MARK_ROIC_BELOW_FLOOR not in result.marks


def test_t3_reads_fail_below_from_the_stage1_thresholds_not_from_a_second_value():
    series = roic_series(0.0)  # ROIC 0.10
    assert MARK_ROIC_BELOW_FLOOR not in evaluate(series).marks
    assert MARK_ROIC_BELOW_FLOOR in evaluate(series, roic=replace_roic(fail_below=0.11, clean_pass_above=0.2)).marks


def test_t3_a_grey_zone_last_year_roic_is_not_marked():
    # 15 / 140 = 10.7 %: between fail_below and clean_pass_above
    assert MARK_ROIC_BELOW_FLOOR not in evaluate(roic_series(-10.0)).marks


# ------------------------------------------------------------------ results


def test_marked_wins_over_not_computable_without_hiding_the_reason():
    periods = periods_from([{}, {}, {"net_debt": None}])
    periods[-1]["revenue"] = 60  # T1 marks (60 / 86.7 < 0.9) while T3 is not computable
    result = evaluate(periods)
    assert MARK_REVENUE_FALLING in result.marks and result.status == STATUS_MARKED
    assert result.roic_invalid_reason == "net_debt_unreliable" and result.roic_last_year is None


def test_not_evaluable_only_without_any_mark():
    periods = periods_from([{}, {}, {"net_debt": None}])
    result = evaluate(periods)
    assert result.marks == () and result.status == STATUS_NOT_EVALUABLE


def test_mark_order_is_fixed_t1_t2_t3_t4():
    result = evaluate(fixture_periods("WEYL19"))
    assert result.marks == (MARK_REVENUE_FALLING, MARK_MARGIN_FALLING, MARK_ROIC_BELOW_FLOOR)
    assert result.mark_count == 3


def test_input_order_has_no_influence():
    periods = fixture_periods("HOOL07")
    assert evaluate(periods) == evaluate(list(reversed(periods)))


def test_the_input_is_not_modified():
    periods = fixture_periods("HOOL07")
    before = [dict(p) for p in periods]
    evaluate(periods)
    assert periods == before


def test_no_periods_and_stale_and_insufficient_years_make_every_signal_not_computable():
    for periods, as_of_year, reason in [
        ([], AS_OF_YEAR, "no_periods"),
        (fixture_periods("ACME01"), 2030, "trend_fundamentals_stale"),
        (fixture_periods("ORBT18"), AS_OF_YEAR, "trend_insufficient_years"),
    ]:
        result = evaluate(periods, as_of_year=as_of_year)
        assert result.status == STATUS_NOT_EVALUABLE and result.marks == ()
        assert (result.revenue_invalid_reason, result.margin_invalid_reason, result.roic_invalid_reason) == (reason,) * 3


def test_a_longer_window_uses_the_configured_number_of_years():
    periods = periods_from([{"revenue": 100}] * 4 + [{"revenue": 85}])
    three = evaluate(periods, config=make_config(years=3))
    five = evaluate(periods, config=make_config(years=5))
    assert three.revenue_ratio != five.revenue_ratio


def test_not_evaluable_trend_result_carries_the_reason_on_every_signal():
    result = not_evaluable_trend_result("fundamentals_cache_missing")
    assert result.status == STATUS_NOT_EVALUABLE and result.marks == ()
    assert (result.revenue_invalid_reason, result.margin_invalid_reason, result.roic_invalid_reason) == ("fundamentals_cache_missing",) * 3
    assert result.revenue_ratio is None and result.ebitda_margin_ratio is None and result.roic_last_year is None


# ------------------------------------------------------------------ pilot-fixture example table (decision text, Abschnitt 5b)


PILOT_TABLE = [
    # fixture, revenue ratio, margin ratio, last-year ROIC, marks, status
    ("ACME01", 1.056, 1.021, 0.139, (), STATUS_CLEAR),
    ("GLBX02", 1.133, 1.050, 0.275, (), STATUS_CLEAR),
    ("STRK05", 1.054, 1.031, 0.134, (), STATUS_CLEAR),
    ("FOGG27", 1.032, 1.002, 0.135, (), STATUS_CLEAR),
    ("HOOL07", 0.950, 0.658, -0.010, (MARK_MARGIN_FALLING, MARK_ROIC_BELOW_FLOOR), STATUS_MARKED),
    ("MDYN06", 0.930, 0.798, 0.034, (MARK_MARGIN_FALLING, MARK_ROIC_BELOW_FLOOR), STATUS_MARKED),
    ("WONK08", 0.969, 0.872, -0.027, (MARK_MARGIN_FALLING, MARK_ROIC_BELOW_FLOOR), STATUS_MARKED),
    ("WEYL19", 0.830, -1.580, -0.130, (MARK_REVENUE_FALLING, MARK_MARGIN_FALLING, MARK_ROIC_BELOW_FLOOR), STATUS_MARKED),
    ("OMNI28", 1.032, 0.531, None, (MARK_MARGIN_FALLING,), STATUS_MARKED),
    ("APEX29", 1.016, 1.306, None, (MARK_MARGIN_PEAK,), STATUS_MARKED),
]


@pytest.mark.parametrize("company,revenue,margin,roic,marks,status", PILOT_TABLE)
def test_pilot_fixture_table(company, revenue, margin, roic, marks, status):
    result = evaluate(fixture_periods(company))
    assert float(result.revenue_ratio) == pytest.approx(revenue, abs=1e-3)
    assert float(result.ebitda_margin_ratio) == pytest.approx(margin, abs=1e-3)
    if roic is None:
        assert result.roic_last_year is None and result.roic_invalid_reason is not None
    else:
        assert result.roic_last_year == pytest.approx(roic, abs=1e-3)
    assert result.marks == marks and result.status == status


def test_pilot_invalid_roic_reasons():
    assert evaluate(fixture_periods("OMNI28")).roic_invalid_reason == "ic_below_asset_floor"
    assert evaluate(fixture_periods("APEX29")).roic_invalid_reason == "roic_implausibly_high"


def test_pilot_void32_revenue_zero_and_orbt18_two_years_are_not_evaluable():
    void = evaluate(fixture_periods("VOID32"))
    assert void.status == STATUS_NOT_EVALUABLE and void.revenue_invalid_reason == "revenue_non_positive"
    assert void.margin_invalid_reason == "revenue_non_positive"
    orbt = evaluate(fixture_periods("ORBT18"))
    assert orbt.status == STATUS_NOT_EVALUABLE and orbt.revenue_invalid_reason == "trend_insufficient_years"

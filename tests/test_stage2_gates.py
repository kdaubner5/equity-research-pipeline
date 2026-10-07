"""Stage-2 evaluation: peer base, classification, combination, info marks and rank
(docs/decisions.md "Stufe 2: Peer-Gruppen und relative Bewertung", Abschnitte 1-4 und 6,
Test 3). Artificial data only - no data/, no database, no network.

Helper figures: market cap 1000, EBITDA 100, so EV/EBITDA = (1000 + net_debt)/100 and
net_debt = 100*m - 1000 gives the multiple m. FCF = CFO - 30 per year, FCF-yield =
(CFO - 30)/1000 (exact in floating point for the round values used here).
"""

import dataclasses
from datetime import date

import pytest

from gates_layer.stage1_gates import (
    FAILED,
    NOT_EVALUABLE,
    PASSED,
    PASSED_WITH_FLAGS,
    UNSUPPORTED_BUSINESS_MODEL,
    GateResult,
)
from gates_layer.stage2_gates import ValuationInput, evaluate_stage2, not_evaluable_result, peer_base_size, rank_stage2
from stage2_helpers import make_config, period

CONFIG = make_config()
_UNSET = object()  # net_debt=None must stay usable as "net debt unreliable"
AS_OF = date(2026, 9, 24)
FINE = 107002  # a cyclical sector in the test config
OTHER = 110001  # not cyclical in the test config
STANDALONE = 105001  # no peers at all -> absolute level


def company(
    ticker,
    sector=OTHER,
    *,
    multiple=5.0,
    cfo=100.0,
    capex=-30.0,
    market_cap=1000.0,
    ebitda=100.0,
    net_debt=_UNSET,
    last_year=2025,
    years=3,
    supported=True,
    borderline=None,
    capex_by_year=None,
    net_debt_flags=(),
):
    """A ValuationInput whose latest-year EV/EBITDA is `multiple` (unless net_debt is given)."""
    if net_debt is _UNSET:
        net_debt = ebitda * multiple - market_cap
    periods = [
        period(last_year - years + 1 + i, ebitda=ebitda, net_debt=net_debt, cfo=cfo, flags=net_debt_flags)
        for i in range(years)
    ]
    if capex_by_year is None:
        capex_by_year = {p["fiscal_year"]: capex for p in periods}
    return ValuationInput(
        ticker=ticker,
        sector_code=sector,
        market_cap_usd=market_cap,
        periods=periods,
        capex_by_year=capex_by_year,
        profile_supported=supported,
        borderline_sector=borderline,
    )


def group(sector, multiples, *, prefix=None, **kwargs):
    """{ticker: ValuationInput} for one sector, one company per multiple."""
    prefix = prefix or f"G{sector}_"
    return {f"{prefix}{i}": company(f"{prefix}{i}", sector, multiple=m, **kwargs) for i, m in enumerate(multiples)}


def s1(status=PASSED, flags=()):
    return GateResult(
        status=status,
        flags=tuple(flags),
        invalid_reasons=(),
        roic=0.2,
        net_debt_ebitda=1.0,
        cash_conversion=1.1,
        net_income_margin=0.1,
    )


def run(inputs, survivors, config=CONFIG):
    stage1 = survivors if isinstance(survivors, dict) else {t: s1() for t in survivors}
    return evaluate_stage2(inputs, stage1, as_of=AS_OF, config=config)


def one_survivor_in_a_group(multiples, survivor_index, **survivor_kwargs):
    """A fine group of len(multiples) peers; the one at survivor_index is the Stage-1 survivor."""
    inputs = group(OTHER, multiples)
    ticker = f"G{OTHER}_{survivor_index}"
    if survivor_kwargs:
        inputs[ticker] = company(ticker, OTHER, multiple=multiples[survivor_index], **survivor_kwargs)
    return inputs, ticker


# ---------------------------------------------------------------- relative classification


def test_survivor_exactly_on_the_tercile_boundary_is_clean():
    # n = 10, rank 4 -> percentile 3/9 = exactly 1/3
    inputs, ticker = one_survivor_in_a_group([5.0 + i for i in range(10)], 3)
    result = run(inputs, [ticker])[ticker]
    assert result.status == PASSED
    assert result.peer_level == "fine"
    assert result.peer_rank == 4
    assert result.peer_n == 10
    assert result.ev_ebitda_percentile == pytest.approx(1 / 3)
    assert result.flags == ()


def test_survivor_just_above_the_tercile_boundary_is_flagged():
    # n = 15, rank 6 -> 5/14 = 0.357 (above 1/3, below 0.40)
    inputs, ticker = one_survivor_in_a_group([5.0 + i for i in range(15)], 5)
    result = run(inputs, [ticker])[ticker]
    assert result.status == PASSED_WITH_FLAGS
    assert result.flags == ("ev_ebitda",)


def test_survivor_exactly_on_the_grey_zone_edge_is_still_flagged():
    # n = 11, rank 5 -> 4/10 = exactly 0.40
    inputs, ticker = one_survivor_in_a_group([5.0 + i for i in range(11)], 4)
    result = run(inputs, [ticker])[ticker]
    assert result.ev_ebitda_percentile == pytest.approx(0.4)
    assert result.status == PASSED_WITH_FLAGS
    assert result.flags == ("ev_ebitda",)


def test_survivor_just_beyond_the_grey_zone_fails():
    # n = 11, rank 6 -> 5/10 = 0.50
    inputs, ticker = one_survivor_in_a_group([5.0 + i for i in range(11)], 5)
    result = run(inputs, [ticker])[ticker]
    assert result.status == FAILED
    assert result.invalid_reasons == ()


def test_the_cheapest_company_of_a_group_is_clean_and_the_dearest_fails():
    multiples = [5.0 + i for i in range(8)]
    inputs = group(OTHER, multiples)
    results = run(inputs, list(inputs))
    assert results[f"G{OTHER}_0"].status == PASSED
    assert results[f"G{OTHER}_7"].status == FAILED


def test_the_tercile_denominator_comes_from_the_config():
    config = make_config(ev_ebitda={"peer_terciles": 2, "grey_zone_upper_percentile": 0.6})
    inputs, ticker = one_survivor_in_a_group([5.0 + i for i in range(11)], 5)  # percentile 0.5
    assert run(inputs, [ticker], config)[ticker].status == PASSED


# ---------------------------------------------------------------- peer base


def test_peers_that_failed_stage_1_still_count_as_peers():
    inputs = group(OTHER, [5.0 + i for i in range(8)])
    survivor = f"G{OTHER}_0"  # the other seven are not in stage1_results at all
    result = run(inputs, [survivor])[survivor]
    assert result.peer_level == "fine"
    assert result.peer_n == 8


def test_only_stage_1_survivors_are_evaluated():
    inputs = group(OTHER, [5.0 + i for i in range(8)])
    stage1 = {
        f"G{OTHER}_0": s1(PASSED),
        f"G{OTHER}_1": s1(PASSED_WITH_FLAGS, ("roic",)),
        f"G{OTHER}_2": s1(FAILED),
        f"G{OTHER}_3": s1(NOT_EVALUABLE),
        f"G{OTHER}_4": s1(UNSUPPORTED_BUSINESS_MODEL),
    }
    results = run(inputs, stage1)
    assert set(results) == {f"G{OTHER}_0", f"G{OTHER}_1"}
    assert results[f"G{OTHER}_0"].peer_n == 8  # the failed, not-evaluable and unsupported-status companies still count as peers


def test_a_company_with_an_invalid_multiple_is_not_part_of_the_base():
    inputs = group(OTHER, [5.0 + i for i in range(7)])
    inputs["BAD"] = company("BAD", OTHER, net_debt=None, net_debt_flags=("net_debt_unreliable",))
    result = run(inputs, [f"G{OTHER}_0"])[f"G{OTHER}_0"]
    assert result.peer_level == "absolute"  # 7 valid multiples are below the minimum on both levels
    assert (result.peer_n_fine, result.peer_n_coarse) == (7, 7)


def test_a_company_with_an_unsupported_profile_is_not_part_of_the_base():
    inputs = group(OTHER, [5.0 + i for i in range(7)])
    inputs["BANK"] = company("BANK", OTHER, multiple=6.0, supported=False)
    result = run(inputs, [f"G{OTHER}_0"])[f"G{OTHER}_0"]
    assert result.peer_n_fine == 7


def test_a_company_without_a_sector_code_is_not_part_of_any_group():
    inputs = group(OTHER, [5.0 + i for i in range(7)])
    inputs["NOSECTOR"] = company("NOSECTOR", None, multiple=6.0)
    result = run(inputs, [f"G{OTHER}_0"])[f"G{OTHER}_0"]
    assert result.peer_n_fine == 7 and result.peer_n_coarse == 7


def test_a_survivor_without_a_sector_code_is_not_evaluable_with_an_explicit_reason():
    inputs = {"X": company("X", None, multiple=5.0)}
    result = run(inputs, ["X"])["X"]
    assert result.status == NOT_EVALUABLE
    assert result.invalid_reasons == ("no_sector_code",)
    assert result.ev_ebitda is None
    assert result.peer_level is None


def test_a_survivor_with_an_unsupported_profile_is_not_evaluable_with_an_explicit_reason():
    inputs = {"X": company("X", OTHER, supported=False)}
    result = run(inputs, ["X"])["X"]
    assert result.status == NOT_EVALUABLE
    assert result.invalid_reasons == ("profile_unsupported",)


def test_a_survivor_without_valuation_input_is_not_skipped():
    result = run({}, ["GONE"])["GONE"]
    assert result.status == NOT_EVALUABLE
    assert result.invalid_reasons == ("valuation_input_missing",)
    assert result.ev_ebitda is None and result.fcf_yield is None
    assert result.peer_level is None
    assert result.info_flags == ()


def test_input_key_and_ticker_must_agree():
    with pytest.raises(ValueError, match="KEY"):
        evaluate_stage2({"KEY": company("OTHER")}, {"KEY": s1()}, as_of=AS_OF, config=CONFIG)


def test_results_do_not_depend_on_the_order_of_the_inputs():
    inputs = group(OTHER, [9.0, 5.0, 7.0, 12.0, 6.0, 8.0, 11.0, 10.0, 7.0])
    survivors = list(inputs)
    forward = run(inputs, survivors)
    backward = run(dict(reversed(list(inputs.items()))), survivors)
    assert forward == backward


def test_nothing_is_written_back_into_the_inputs():
    inputs = group(OTHER, [5.0 + i for i in range(8)])
    before = {t: (list(i.periods), dict(i.capex_by_year)) for t, i in inputs.items()}
    run(inputs, list(inputs))
    assert before == {t: (list(i.periods), dict(i.capex_by_year)) for t, i in inputs.items()}


# ---------------------------------------------------------------- fallback levels and absolute classification


def test_coarse_fallback_is_reported_and_marked():
    inputs = {**group(107002, [5.0, 6.0, 7.0, 8.0]), **group(107004, [5.5, 6.5, 7.5, 8.5])}
    ticker = "G107002_0"
    result = run(inputs, [ticker])[ticker]
    assert result.peer_level == "coarse"
    assert result.peer_group == 107
    assert result.peer_n == 8
    assert (result.peer_n_fine, result.peer_n_coarse) == (4, 8)
    assert "peer_group_coarse" in result.info_flags
    assert result.status == PASSED  # info marks never change the status


@pytest.mark.parametrize(
    "multiple,status",
    [(7.99, PASSED), (8.0, PASSED_WITH_FLAGS), (9.99, PASSED_WITH_FLAGS), (10.0, FAILED), (14.0, FAILED)],
)
def test_absolute_level_uses_the_absolute_thresholds(multiple, status):
    inputs = {"X": company("X", STANDALONE, multiple=multiple)}
    result = run(inputs, ["X"])["X"]
    assert result.peer_level == "absolute"
    assert result.peer_group is None
    assert result.peer_rank is None and result.peer_n is None and result.ev_ebitda_percentile is None
    assert "peer_group_absolute" in result.info_flags
    assert result.status == status
    assert result.flags == (("ev_ebitda",) if status == PASSED_WITH_FLAGS else ())


# ---------------------------------------------------------------- FCF-yield classification (absolute level so only the yield varies)


@pytest.mark.parametrize(
    "cfo,status",
    [
        (100.0, PASSED),  # 7.0 %
        (91.0, PASSED),  # 6.1 %
        (90.0, PASSED_WITH_FLAGS),  # exactly 6 %: the Masterplan says "> 6 %", strict
        (80.0, PASSED_WITH_FLAGS),  # exactly 5 %: the lower edge of the grey zone is inclusive
        (79.0, FAILED),  # 4.9 %
        (-100.0, FAILED),  # negative free cash flow is a valid, failing value
    ],
)
def test_fcf_yield_boundaries(cfo, status):
    inputs = {"X": company("X", STANDALONE, multiple=5.0, cfo=cfo)}
    result = run(inputs, ["X"])["X"]
    assert result.status == status
    assert result.fcf_yield == pytest.approx((cfo - 30.0) / 1000.0)
    assert result.flags == (("fcf_yield",) if status == PASSED_WITH_FLAGS else ())


def test_both_metrics_in_the_grey_zone_list_both_flags_in_a_fixed_order():
    inputs = {"X": company("X", STANDALONE, multiple=9.0, cfo=90.0)}
    assert run(inputs, ["X"])["X"].flags == ("ev_ebitda", "fcf_yield")


# ---------------------------------------------------------------- combination with invalid metrics


def test_a_valid_fail_beats_an_invalid_other_metric():
    # EV/EBITDA unreliable (net debt None) but FCF-yield 4 % -> failed, the reason stays on record
    inputs = {"X": company("X", STANDALONE, net_debt=None, cfo=70.0, net_debt_flags=("net_debt_unreliable",))}
    result = run(inputs, ["X"])["X"]
    assert result.status == FAILED
    assert result.invalid_reasons == ("net_debt_unreliable",)
    assert result.flags == ()


def test_a_valid_ev_ebitda_fail_beats_a_missing_capex():
    inputs = {"X": company("X", STANDALONE, multiple=14.0, capex_by_year={})}
    result = run(inputs, ["X"])["X"]
    assert result.status == FAILED
    assert result.invalid_reasons == ("capex_missing",)


def test_an_invalid_metric_with_only_good_or_grey_results_is_not_evaluable():
    inputs = {"X": company("X", STANDALONE, multiple=5.0, capex_by_year={})}
    result = run(inputs, ["X"])["X"]
    assert result.status == NOT_EVALUABLE
    assert result.invalid_reasons == ("capex_missing",)
    assert result.flags == ()
    assert result.ev_ebitda == pytest.approx(5.0)  # the valid raw value is kept
    assert result.fcf_yield is None


def test_two_invalid_metrics_list_both_reasons_in_metric_order():
    inputs = {"X": company("X", STANDALONE, net_debt=None, capex_by_year={})}
    result = run(inputs, ["X"])["X"]
    assert result.status == NOT_EVALUABLE
    assert result.invalid_reasons == ("net_debt_unreliable", "capex_missing")


def test_stale_fundamentals_make_both_metrics_invalid():
    inputs = {"X": company("X", STANDALONE, last_year=2024)}
    result = run(inputs, ["X"])["X"]
    assert result.status == NOT_EVALUABLE
    assert result.invalid_reasons == ("fundamentals_stale", "fundamentals_stale")


def test_missing_market_cap_makes_both_metrics_invalid():
    inputs = {"X": dataclasses.replace(company("X", STANDALONE), market_cap_usd=None)}
    result = run(inputs, ["X"])["X"]
    assert result.status == NOT_EVALUABLE
    assert result.invalid_reasons == ("market_cap_missing", "market_cap_missing")


# ---------------------------------------------------------------- info marks never change the outcome


def test_cyclical_sector_is_marked_but_changes_nothing():
    cyc = group(FINE, [5.0 + i for i in range(8)])
    non = group(OTHER, [5.0 + i for i in range(8)])
    result_cyc = run(cyc, [f"G{FINE}_0"])[f"G{FINE}_0"]
    result_non = run(non, [f"G{OTHER}_0"])[f"G{OTHER}_0"]
    assert "cyclical_sector" in result_cyc.info_flags
    assert "cyclical_sector" not in result_non.info_flags
    assert (result_cyc.status, result_cyc.flags, result_cyc.invalid_reasons) == (
        result_non.status,
        result_non.flags,
        result_non.invalid_reasons,
    )


def test_cyclical_codes_come_from_the_config():
    config = make_config(cyclical_sectors={"sector_codes": ()})
    inputs = group(FINE, [5.0 + i for i in range(8)])
    assert "cyclical_sector" not in run(inputs, [f"G{FINE}_0"], config)[f"G{FINE}_0"].info_flags


def test_borderline_marker_is_passed_through_and_changes_nothing():
    inputs = {"X": company("X", STANDALONE, borderline=True), "Y": company("Y", STANDALONE, borderline=False)}
    results = run(inputs, ["X", "Y"])
    assert "borderline_sector" in results["X"].info_flags
    assert "borderline_sector" not in results["Y"].info_flags
    assert results["X"].status == results["Y"].status


def test_capex_net_inflow_is_marked_and_does_not_create_cash():
    inputs = {"X": company("X", STANDALONE, multiple=5.0, cfo=100.0, capex_by_year={2023: -30.0, 2024: 500.0, 2025: -30.0})}
    result = run(inputs, ["X"])["X"]
    assert "capex_net_inflow_treated_as_zero" in result.info_flags
    assert result.fcf_yield == pytest.approx((70 + 100 + 70) / 3 / 1000)


def test_no_inflow_mark_when_the_yield_is_invalid():
    inputs = {"X": company("X", STANDALONE, capex_by_year={2023: 500.0, 2024: None, 2025: -30.0})}
    result = run(inputs, ["X"])["X"]
    assert result.invalid_reasons == ("capex_missing",)
    assert "capex_net_inflow_treated_as_zero" not in result.info_flags


def test_info_marks_have_a_fixed_order():
    inputs = {**group(FINE, [5.0, 6.0, 7.0])}
    inputs["G107002_0"] = company("G107002_0", FINE, multiple=5.0, borderline=True, capex_by_year={2023: -1, 2024: 9, 2025: -1})
    result = run(inputs, ["G107002_0"])["G107002_0"]
    assert result.info_flags == (
        "cyclical_sector",
        "capex_net_inflow_treated_as_zero",
        "peer_group_absolute",
        "borderline_sector",
        "share_count_unchecked",  # no dividend data in this test company (docs/decisions.md "Datenplausibilitaet")
    )


# ---------------------------------------------------------------- normalized multiple (information only)


def test_normalized_multiple_is_reported_and_never_changes_the_status():
    peak = company("X", STANDALONE, multiple=5.0)
    peak.periods[0]["ebitda"] = 10.0
    peak.periods[1]["ebitda"] = 20.0
    peak.periods[2]["ebitda"] = 270.0  # net debt stays -500 (built for EBITDA 100), so EV = 1000 - 500 = 500
    result = run({"X": peak}, ["X"])["X"]
    assert result.ev_ebitda == pytest.approx(500 / 270)
    assert result.ev_ebitda_normalized == pytest.approx(500 / 100)  # mean EBITDA (10 + 20 + 270)/3 = 100
    assert result.ev_ebitda_normalized_invalid_reason is None
    assert result.status == PASSED


def test_normalized_multiple_failure_is_visible_not_silent():
    short = company("X", STANDALONE, multiple=5.0, years=2, capex_by_year=None)
    result = run({"X": short}, ["X"])["X"]
    assert result.ev_ebitda_normalized is None
    assert result.ev_ebitda_normalized_invalid_reason == "insufficient_years"
    assert result.invalid_reasons == ("insufficient_years",)  # the FCF-yield needs three years too
    assert result.status == NOT_EVALUABLE


def test_normalized_multiple_invalid_reason_does_not_become_an_invalid_reason_of_the_company():
    config = make_config(fcf_yield={"years": 2})
    short = company("X", STANDALONE, multiple=5.0, years=2)
    result = run({"X": short}, ["X"], config)["X"]
    assert result.ev_ebitda_normalized_invalid_reason == "insufficient_years"
    assert result.invalid_reasons == ()
    assert result.status == PASSED


# ---------------------------------------------------------------- rank


def _ranked(stage1, inputs):
    results = evaluate_stage2(inputs, stage1, as_of=AS_OF, config=CONFIG)
    return [t for t, _ in rank_stage2(stage1, results)]


def test_rank_orders_by_total_flags_then_percentile_then_ticker():
    # three-company absolute-level cases cannot give percentiles, so use one 12-company group
    multiples = [5.0 + i * 0.1 for i in range(12)]
    inputs = group(OTHER, multiples)
    names = [f"G{OTHER}_{i}" for i in range(12)]
    stage1 = {
        names[1]: s1(PASSED_WITH_FLAGS, ("roic",)),  # one Stage-1 flag
        names[0]: s1(PASSED),
        names[2]: s1(PASSED),
        names[3]: s1(PASSED),
    }
    order = _ranked(stage1, inputs)
    # total flags: names[0] 0, names[2] 0 (percentile 2/11 = 0.18 -> clean), names[3] 0 (0.27 clean), names[1] 1
    assert order == [names[0], names[2], names[3], names[1]]


def test_rank_counts_stage_2_flags_too():
    inputs = {**group(OTHER, [5.0 + i for i in range(11)])}  # rank 5 of 11 is exactly 0.40 -> flag
    flagged, clean = f"G{OTHER}_4", f"G{OTHER}_3"
    order = _ranked({flagged: s1(PASSED), clean: s1(PASSED)}, inputs)
    assert order == [clean, flagged]


def test_rank_puts_absolute_level_companies_after_percentile_companies_of_the_same_flag_count():
    inputs = {**group(OTHER, [5.0 + i for i in range(8)]), "AAA": company("AAA", STANDALONE, multiple=5.0)}
    survivor_in_group = f"G{OTHER}_7"  # percentile 1.0 but still failed -> excluded; use rank 3 instead
    assert survivor_in_group not in _ranked({survivor_in_group: s1()}, inputs)
    inside = f"G{OTHER}_2"
    assert _ranked({"AAA": s1(), inside: s1()}, inputs) == [inside, "AAA"]


def test_rank_breaks_ties_by_ticker():
    inputs = {"B": company("B", STANDALONE, multiple=5.0), "A": company("A", STANDALONE, multiple=5.0)}
    assert _ranked({"B": s1(), "A": s1()}, inputs) == ["A", "B"]


def test_rank_excludes_failed_and_not_evaluable_stage_2_results():
    inputs = {
        "OK": company("OK", STANDALONE, multiple=5.0),
        "FAIL": company("FAIL", STANDALONE, multiple=14.0),
        "NOEVAL": company("NOEVAL", STANDALONE, capex_by_year={}),
    }
    assert _ranked({t: s1() for t in inputs}, inputs) == ["OK"]


def test_rank_with_a_stage_2_result_that_has_no_stage_1_result_is_a_caller_error():
    inputs = {"X": company("X", STANDALONE)}
    results = evaluate_stage2(inputs, {"X": s1()}, as_of=AS_OF, config=CONFIG)
    with pytest.raises(ValueError, match="X"):
        rank_stage2({}, results)


# ---------------------------------------------------------------- helpers used by the runner


def test_not_evaluable_result_carries_one_reason_and_no_values():
    result = not_evaluable_result("fundamentals_cache_missing")
    assert result.status == NOT_EVALUABLE
    assert result.invalid_reasons == ("fundamentals_cache_missing",)
    assert result.flags == () and result.info_flags == ()
    assert result.ev_ebitda is None and result.fcf_yield is None and result.peer_level is None


def test_peer_base_size_counts_only_supported_valid_companies_with_a_sector():
    inputs = group(OTHER, [5.0 + i for i in range(8)])
    inputs["BANK"] = company("BANK", OTHER, multiple=6.0, supported=False)
    inputs["BAD"] = company("BAD", OTHER, net_debt=None)
    inputs["NOSECTOR"] = company("NOSECTOR", None, multiple=6.0)
    assert peer_base_size(inputs, as_of=AS_OF, config=CONFIG) == 8

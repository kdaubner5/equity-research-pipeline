"""Stage-2 gates with the two plausibility checks (docs/decisions.md "Stufe 2 -
Datenplausibilitaet: Aktienzahl und Investitionen", Abschnitte 1, 2 und 5, Test 2).

Builders come from test_stage2_gates.py: market cap 1000, EBITDA 100, three years 2023-2025, FCF
= CFO - 30 (yield (CFO - 30)/1000). Dividend data is added with dataclasses.replace.
"""

import dataclasses

import pytest

from gates_layer.stage1_gates import FAILED, NOT_EVALUABLE, PASSED, PASSED_WITH_FLAGS
from gates_layer.stage2_gates import (
    SHARE_AMBIGUOUS,
    SHARE_OK,
    SHARE_SUSPECT,
    SHARE_UNCHECKED,
    assess_share_counts,
    evaluate_stage2,
    peer_base_summary,
    rank_stage2,
)
from stage2_helpers import make_config
from test_stage2_gates import AS_OF, CONFIG, OTHER, STANDALONE, company, group, run, s1


def with_dividend(vi, *, paid=-100.0, shares=25.0, dps=1.0, year=2025):
    """Dividend inputs; R = (|paid| / shares) / dps (-100, 25, 1.0 -> R = 4)."""
    return dataclasses.replace(vi, dividends_paid_by_year={year: paid}, shares_outstanding=shares, dividend_per_share=dps)


def with_other_investing(vi, other):
    return dataclasses.replace(vi, other_investing_by_year=other)


# ---------------------------------------------------------------- share-count assessment


def test_assessment_verdicts_follow_the_ratio():
    base = company("X", STANDALONE)
    cases = {
        "ok": (with_dividend(base), SHARE_OK, 4.0),
        "suspect": (with_dividend(base, shares=5.0), SHARE_SUSPECT, 20.0),
        "ambiguous": (with_dividend(base, paid=-1200.0, shares=100.0, dps=1.0), SHARE_AMBIGUOUS, 12.0),
    }
    for name, (vi, verdict, ratio) in cases.items():
        assessment = assess_share_counts({"X": vi}, CONFIG)["X"]
        assert assessment.verdict == verdict, name
        assert assessment.ratio == pytest.approx(ratio)


def test_assessment_exactly_on_the_threshold_is_ok_and_just_above_is_suspect():
    base = company("X", STANDALONE)
    assert assess_share_counts({"X": with_dividend(base, paid=-600.0, shares=100.0, dps=1.0)}, CONFIG)["X"].verdict == SHARE_OK
    assert assess_share_counts({"X": with_dividend(base, paid=-600.0, shares=100.0, dps=0.99)}, CONFIG)["X"].verdict == SHARE_SUSPECT


def test_assessment_uses_the_dividend_of_the_latest_fiscal_year_only():
    base = company("X", STANDALONE)
    vi = dataclasses.replace(with_dividend(base, year=2023), dividends_paid_by_year={2023: -100.0})  # none for 2025
    assessment = assess_share_counts({"X": vi}, CONFIG)["X"]
    assert assessment.verdict == SHARE_UNCHECKED and assessment.reason == "dividend_data_missing"


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"paid": None}, "dividend_data_missing"),
        ({"paid": 0}, "dividend_not_positive"),
        ({"dps": None}, "dividend_data_missing"),
        ({"dps": 0.0}, "dividend_not_positive"),
        ({"shares": None}, "dividend_data_missing"),
        ({"shares": 0.0}, "shares_not_positive"),
        ({"paid": float("nan")}, "non_finite_input"),
        ({"shares": 10**400}, "non_finite_input"),
    ],
)
def test_assessment_without_usable_dividend_data_is_unchecked_with_its_reason(kwargs, reason):
    assessment = assess_share_counts({"X": with_dividend(company("X", STANDALONE), **kwargs)}, CONFIG)["X"]
    assert (assessment.verdict, assessment.reason, assessment.ratio) == (SHARE_UNCHECKED, reason, None)


def test_a_company_without_any_dividend_data_is_unchecked():
    assessment = assess_share_counts({"X": company("X", STANDALONE)}, CONFIG)["X"]
    assert assessment.verdict == SHARE_UNCHECKED


def test_a_company_without_periods_is_unchecked():
    vi = dataclasses.replace(company("X", STANDALONE), periods=[])
    assert assess_share_counts({"X": vi}, CONFIG)["X"].verdict == SHARE_UNCHECKED


def test_unsupported_profiles_are_not_assessed():
    assert assess_share_counts({"B": company("B", STANDALONE, supported=False)}, CONFIG) == {}


# ---------------------------------------------------------------- suspect share counts in the evaluation


def test_a_suspect_survivor_is_not_evaluable_with_its_own_reason_and_keeps_the_ratio():
    inputs = {"X": with_dividend(company("X", STANDALONE), shares=5.0)}
    result = run(inputs, ["X"])["X"]
    assert result.status == NOT_EVALUABLE
    assert result.invalid_reasons == ("share_count_suspect",)
    assert result.share_count_ratio == pytest.approx(20.0)
    assert result.ev_ebitda is None and result.fcf_yield is None and result.flags == ()


def test_a_suspect_survivor_is_not_rescued_or_failed_by_the_other_metrics():
    # with a cheap multiple and a fine yield, or an awful one: the verdict on the share count wins either way
    cheap = {"X": with_dividend(company("X", STANDALONE, multiple=5.0, cfo=100.0), shares=5.0)}
    dear = {"X": with_dividend(company("X", STANDALONE, multiple=14.0, cfo=0.0), shares=5.0)}
    assert run(cheap, ["X"])["X"].status == NOT_EVALUABLE
    assert run(dear, ["X"])["X"].status == NOT_EVALUABLE


def test_a_suspect_company_leaves_the_peer_base():
    inputs = group(OTHER, [5.0 + i for i in range(8)])
    survivor = f"G{OTHER}_0"
    assert run(inputs, [survivor])[survivor].peer_level == "fine"
    ticker = f"G{OTHER}_5"
    inputs[ticker] = with_dividend(inputs[ticker], shares=5.0)
    result = run(inputs, [survivor])[survivor]
    assert (result.peer_n_fine, result.peer_n_coarse) == (7, 7)
    assert result.peer_level == "absolute"  # 7 valid peers are below the minimum of 8


def test_a_suspect_company_changes_the_percentiles_of_the_others():
    inputs = group(OTHER, [5.0 + i for i in range(10)])
    survivor = f"G{OTHER}_9"  # dearest of ten -> percentile 1.0
    before = run(inputs, [survivor])[survivor]
    ticker = f"G{OTHER}_0"
    inputs[ticker] = with_dividend(inputs[ticker], shares=5.0)  # the cheapest drops out
    after = run(inputs, [survivor])[survivor]
    assert (before.peer_rank, before.peer_n) == (10, 10)
    assert (after.peer_rank, after.peer_n) == (9, 9)


def test_ambiguous_and_unchecked_companies_stay_evaluable_and_carry_their_marks_after_the_existing_ones():
    ambiguous = {"X": with_dividend(company("X", STANDALONE, borderline=True), paid=-1200.0, shares=100.0, dps=1.0)}
    unchecked = {"X": company("X", STANDALONE, borderline=True)}
    ok = {"X": with_dividend(company("X", STANDALONE, borderline=True))}
    r_amb, r_unc, r_ok = (run(i, ["X"])["X"] for i in (ambiguous, unchecked, ok))
    assert r_amb.info_flags[-1] == "share_count_ambiguous" and r_unc.info_flags[-1] == "share_count_unchecked"
    assert "share_count_ambiguous" not in r_ok.info_flags and "share_count_unchecked" not in r_ok.info_flags
    assert r_amb.info_flags[:2] == ("peer_group_absolute", "borderline_sector")
    assert r_amb.status == r_unc.status == r_ok.status == PASSED  # marks never change the outcome
    assert r_ok.share_count_ratio == pytest.approx(4.0) and r_unc.share_count_ratio is None


def test_the_suspect_threshold_comes_from_the_config():
    inputs = {"X": with_dividend(company("X", STANDALONE), paid=-300.0, shares=50.0, dps=1.0)}  # R = 6
    assert run(inputs, ["X"])["X"].status == PASSED
    strict = make_config(share_count_check={"suspect_ratio_above": 5.0, "monthly_band_low": 9.0, "monthly_band_high": 15.0})
    assert run(inputs, ["X"], strict)["X"].status == NOT_EVALUABLE


def test_peer_base_summary_names_the_companies_excluded_for_a_suspect_share_count():
    inputs = group(OTHER, [5.0 + i for i in range(8)])
    inputs[f"G{OTHER}_3"] = with_dividend(inputs[f"G{OTHER}_3"], shares=5.0)
    inputs["BADDEBT"] = company("BADDEBT", OTHER, net_debt=None)  # invalid anyway: not counted as excluded for shares
    inputs["BANK"] = company("BANK", OTHER, supported=False)
    summary = peer_base_summary(inputs, as_of=AS_OF, config=CONFIG)
    assert summary.size == 7
    assert summary.excluded_share_count_suspect == (f"G{OTHER}_3",)


def test_a_missing_valuation_input_still_gets_its_explicit_reason():
    result = run({}, ["GONE"])["GONE"]
    assert result.invalid_reasons == ("valuation_input_missing",) and result.share_count_ratio is None


# ---------------------------------------------------------------- other investing: flag fcf_yield_unverified

# A company with FCF-yield 9 % (CFO 100, capex -10, market cap 1000) and a multiple of 5 (absolute level, clean).


def clean_company(other, **kwargs):
    vi = company("X", STANDALONE, multiple=5.0, cfo=100.0, capex=-10.0, **kwargs)
    return {"X": with_other_investing(vi, other)}


def test_a_clean_company_whose_counter_calculation_is_not_clean_gets_the_flag_and_is_no_longer_passed():
    result = run(clean_company({2023: -200.0, 2024: -200.0, 2025: -200.0}), ["X"])["X"]
    assert result.status == PASSED_WITH_FLAGS
    assert result.flags == ("fcf_yield_unverified",)
    assert result.other_investing_triggered is True
    assert result.fcf_yield == pytest.approx(0.09)  # the main value is kept
    assert result.fcf_yield_incl_other_investing == pytest.approx((100 - 210) / 1000)


def test_a_counter_calculation_below_the_fail_threshold_still_only_flags():
    result = run(clean_company({2023: -900.0, 2024: -900.0, 2025: -900.0}), ["X"])["X"]
    assert result.fcf_yield_incl_other_investing < CONFIG.fcf_yield.fail_below
    assert (result.status, result.flags) == (PASSED_WITH_FLAGS, ("fcf_yield_unverified",))


@pytest.mark.parametrize(
    "other,flagged",
    [
        ({2023: -30.0, 2024: 0.0, 2025: 0.0}, False),  # ratio 3 > 2: triggered, alt FCF = (60 + 90 + 90)/3 -> 8 %: clean
        ({2023: -30.0, 2024: -30.0, 2025: -30.0}, True),  # alt FCF 60 per year -> exactly 6 %: not 'above' 6 % -> not clean
        ({2023: -29.0, 2024: -29.0, 2025: -29.0}, False),  # alt FCF 61 -> 6.1 %: still clean
    ],
)
def test_the_clean_boundary_of_the_counter_calculation(other, flagged):
    result = run(clean_company(other), ["X"])["X"]
    assert result.other_investing_triggered is True
    assert ("fcf_yield_unverified" in result.flags) is flagged
    assert result.status == (PASSED_WITH_FLAGS if flagged else PASSED)


def test_no_trigger_no_flag_even_if_the_values_would_be_low():
    # ratio exactly 2.0 in all years: not 'more than double'
    result = run(clean_company({2023: -20.0, 2024: -20.0, 2025: -20.0}), ["X"])["X"]
    assert result.other_investing_triggered is False
    assert result.flags == () and result.status == PASSED


def test_unchecked_when_other_investing_is_unavailable():
    result = run(clean_company({}), ["X"])["X"]
    assert result.other_investing_triggered is None
    assert result.fcf_yield_incl_other_investing is None
    assert result.status == PASSED


def test_a_main_yield_already_in_the_grey_zone_gets_no_extra_flag():
    vi = with_other_investing(company("X", STANDALONE, multiple=5.0, cfo=90.0, capex=-30.0), {2023: -500.0, 2024: -500.0, 2025: -500.0})
    result = run({"X": vi}, ["X"])["X"]  # FCF 60 -> 6.0 %: grey zone already
    assert result.flags == ("fcf_yield",)
    assert "fcf_yield_unverified" not in result.flags


def test_a_failed_company_stays_failed_and_gets_no_unverified_flag():
    vi = with_other_investing(company("X", STANDALONE, multiple=14.0, cfo=100.0, capex=-10.0), {2023: -500.0, 2024: -500.0, 2025: -500.0})
    result = run({"X": vi}, ["X"])["X"]
    assert result.status == FAILED and "fcf_yield_unverified" not in result.flags
    assert result.other_investing_triggered is True and result.fcf_yield_incl_other_investing is not None


def test_a_not_evaluable_company_stays_not_evaluable():
    vi = with_other_investing(company("X", STANDALONE, multiple=5.0, capex_by_year={}), {2025: -500.0})
    result = run({"X": vi}, ["X"])["X"]
    assert result.status == NOT_EVALUABLE and result.other_investing_triggered is None


def test_a_non_finite_other_investing_value_makes_the_company_unverified_not_clean():
    # triggered by 2024, but the counter-calculation cannot be computed because of the NaN in 2025
    vi = clean_company({2023: 0.0, 2024: -500.0, 2025: float("nan")})
    result = run(vi, ["X"])["X"]
    assert result.other_investing_triggered is True
    assert result.flags == ("fcf_yield_unverified",) and result.status == PASSED_WITH_FLAGS


def test_the_ratio_comes_from_the_config():
    other = {2023: -30.0, 2024: -30.0, 2025: -30.0}
    strict = make_config(other_investing_check={"min_ratio_to_capex": 4.0})
    assert run(clean_company(other), ["X"], strict)["X"].other_investing_triggered is False


def test_the_unverified_flag_counts_in_the_rank_like_any_flag():
    flagged = clean_company({2023: -200.0, 2024: -200.0, 2025: -200.0})["X"]
    plain = company("A", STANDALONE, multiple=5.0, cfo=100.0, capex=-10.0)
    inputs = {"X": flagged, "A": dataclasses.replace(plain, ticker="A")}
    stage1 = {"X": s1(), "A": s1()}
    results = evaluate_stage2(inputs, stage1, as_of=AS_OF, config=CONFIG)
    assert [t for t, _ in rank_stage2(stage1, results)] == ["A", "X"]  # "X" has one flag more although it sorts first by name


def test_results_do_not_depend_on_input_order_with_the_new_checks():
    inputs = group(OTHER, [9.0, 5.0, 7.0, 12.0, 6.0, 8.0, 11.0, 10.0])
    inputs[f"G{OTHER}_1"] = with_dividend(inputs[f"G{OTHER}_1"], shares=5.0)
    inputs[f"G{OTHER}_2"] = with_other_investing(inputs[f"G{OTHER}_2"], {2023: -500.0, 2024: -500.0, 2025: -500.0})
    survivors = list(inputs)
    assert run(inputs, survivors) == run(dict(reversed(list(inputs.items()))), survivors)

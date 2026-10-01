import pytest

from conftest import DEFAULT_THRESHOLDS, load_company
from metrics_layer.cash_conversion import cash_conversion_3yr_average, net_income_margin_3yr_average
from metrics_layer.net_debt_ebitda import net_debt_ebitda_latest
from metrics_layer.result import MetricResult
from metrics_layer.roic import roic_3yr_average
import dataclasses

from gates_layer.stage1_gates import (
    UNSUPPORTED_BUSINESS_MODEL,
    collect_data_quality_flags,
    evaluate_stage1_gates,
    rank_companies,
    unsupported_business_model_result,
)


def _gate_result_for(company_id):
    company = load_company(company_id)
    periods = company["periods"]
    return evaluate_stage1_gates(
        roic=roic_3yr_average(periods, thresholds=DEFAULT_THRESHOLDS.roic),
        net_debt_ebitda=net_debt_ebitda_latest(periods),
        cash_conversion=cash_conversion_3yr_average(periods),
        net_income_margin=net_income_margin_3yr_average(periods),
        thresholds=DEFAULT_THRESHOLDS,
    )


def test_evaluate_stage1_gates_clean_pass():
    result = _gate_result_for("ACME01")
    assert result.status == "passed"
    assert result.flags == ()
    assert result.invalid_reasons == ()
    assert result.roic == pytest.approx(0.1343, abs=0.001)
    assert result.net_debt_ebitda == pytest.approx(800 / 790, abs=0.001)
    assert result.cash_conversion == pytest.approx(1.6386, abs=0.001)


@pytest.mark.parametrize(
    "company_id,expected_flags",
    [
        ("APRT11", ("roic",)),  # ROIC grey zone (~11.02%), other two clean pass
        ("SPCL20", ("cash_conversion",)),  # Cash Conversion grey zone (0.90)
        ("VULC14", ("net_debt_ebitda",)),  # Net Debt/EBITDA lower grey-zone boundary (2.30x)
        ("OSCP21", ("cash_conversion",)),  # Cash Conversion lower grey-zone boundary (0.80)
        ("INGN23", ("roic",)),  # ROIC upper grey-zone boundary (exactly 12.0%)
    ],
)
def test_evaluate_stage1_gates_single_flag(company_id, expected_flags):
    result = _gate_result_for(company_id)
    assert result.status == "passed_with_flags"
    assert result.flags == expected_flags
    assert result.invalid_reasons == ()


def test_evaluate_stage1_gates_two_flags_simultaneously():
    result = _gate_result_for("BLKW13")
    assert result.status == "passed_with_flags"
    assert result.flags == ("roic", "net_debt_ebitda")
    assert result.invalid_reasons == ()
    assert result.roic == pytest.approx(0.1095, abs=0.001)
    assert result.net_debt_ebitda == pytest.approx(2.40, abs=0.001)
    assert result.cash_conversion == pytest.approx(1.43, abs=0.01)


def test_evaluate_stage1_gates_roic_lower_boundary_is_flag_not_failed():
    # SIRI24: ROIC exactly 10.0% (3yr avg, every year identically 10.0%).
    # Regression guard for the ROIC fail/flag boundary direction documented
    # in docs/decisions.md: the grey zone is inclusive on its "worse" side
    # (10% <= x <= 12%), analogous to Net Debt/EBITDA's un-named clean-pass
    # edge - so 10.0% itself must classify as a flag, never as failed.
    result = _gate_result_for("SIRI24")
    assert result.status == "passed_with_flags"
    assert result.status != "failed"
    assert result.flags == ("roic",)
    assert result.roic == pytest.approx(0.10, abs=0.001)


@pytest.mark.parametrize(
    "company_id",
    [
        "GEKO12",  # Net Debt/EBITDA hard threshold itself (exactly 2.50x)
        "CYBR15",  # Net Debt/EBITDA clearly above the threshold (2.60x)
    ],
)
def test_evaluate_stage1_gates_failed(company_id):
    result = _gate_result_for(company_id)
    assert result.status == "failed"
    assert result.invalid_reasons == ()
    # ROIC and Cash Conversion are both clean pass in isolation on these
    # fixtures, so the failed status is driven purely by Net Debt/EBITDA
    assert result.flags == ()


def test_evaluate_stage1_gates_clean_pass_at_cash_conversion_upper_boundary():
    # DUFF22: Cash Conversion exactly 1.00 (inclusive threshold) -> clean
    # pass, not grey zone; other two metrics healthy.
    result = _gate_result_for("DUFF22")
    assert result.status == "passed"
    assert result.flags == ()
    assert result.cash_conversion == pytest.approx(1.0, abs=0.001)


def test_evaluate_stage1_gates_point_in_time_not_averaged():
    # TYRL16: Net Debt/EBITDA only looks at the latest fiscal year (2024,
    # 1.0x, healthy) rather than averaging in the much worse 2022/2023
    # values - a wrongly-averaging implementation would compute ~2.5x and
    # fail this company instead of passing it cleanly.
    result = _gate_result_for("TYRL16")
    assert result.status == "passed"
    assert result.flags == ()
    assert result.net_debt_ebitda == pytest.approx(1.0, abs=0.001)


@pytest.mark.parametrize(
    "company_id,expected_reason",
    [
        ("ORBT18", "insufficient_years"),
        ("MURK26", "net_debt_unreliable"),
        ("VOID32", "revenue_non_positive"),
    ],
)
def test_evaluate_stage1_gates_not_evaluable(company_id, expected_reason):
    # Only fixtures whose valid metrics are all outside the hard-fail zone
    # stay not_evaluable. NAKA17, MDYN06 and WEYL19 moved to
    # test_evaluate_stage1_gates_valid_hard_fail_beats_invalid_reason_on_fixtures
    # (docs/decisions.md, "Phase 2.0d", Entscheidung 1).
    result = _gate_result_for(company_id)
    assert result.status == "not_evaluable"
    assert result.flags == ()
    assert expected_reason in result.invalid_reasons


def _in_hard_fail_zone(metric_name, value):
    """Whether value is in the metric's hard-fail zone, derived from DEFAULT_THRESHOLDS
    (independent of the gate's own classifiers, no hard-coded numbers)."""
    if metric_name == "roic":
        return value < DEFAULT_THRESHOLDS.roic.fail_below
    if metric_name == "net_debt_ebitda":
        return value >= DEFAULT_THRESHOLDS.net_debt_ebitda.fail_at_or_above
    if metric_name == "cash_conversion":
        return value < DEFAULT_THRESHOLDS.cash_conversion.fail_below
    raise ValueError(metric_name)


@pytest.mark.parametrize(
    "company_id,expected_reason,hard_fail_metrics",
    [
        ("NAKA17", "ic_non_positive", ("net_debt_ebitda",)),  # roic invalid; Net Debt/EBITDA 2.86x
        ("MDYN06", "ni_non_positive", ("roic", "net_debt_ebitda")),  # cash_conversion invalid
        ("WEYL19", "ebitda_non_positive", ("roic",)),  # net_debt_ebitda and cash_conversion invalid
    ],
)
def test_evaluate_stage1_gates_valid_hard_fail_beats_invalid_reason_on_fixtures(
    company_id, expected_reason, hard_fail_metrics
):
    # Phase 2.0d, Entscheidung 1: a valid metric in the hard-fail zone makes the
    # company failed even though another metric carries an invalid_reason. The
    # guard stays visible in invalid_reasons (Entscheidung 2).
    result = _gate_result_for(company_id)
    assert result.status == "failed"
    assert expected_reason in result.invalid_reasons
    for metric_name in hard_fail_metrics:
        value = getattr(result, metric_name)
        assert value is not None
        assert _in_hard_fail_zone(metric_name, value)


@pytest.mark.parametrize(
    "invalid_metric,reason",
    [
        ("roic", "ic_non_positive"),
        ("net_debt_ebitda", "ebitda_non_positive"),
        ("cash_conversion", "ni_non_positive"),
    ],
)
def test_evaluate_stage1_gates_single_guard_with_healthy_other_metrics_is_not_evaluable(invalid_metric, reason):
    # Gates-level coverage of each guard in isolation (the fixtures for these
    # guards also carry valid hard fails, see the fixture test above).
    inputs = {
        "roic": MetricResult(value=0.15),
        "net_debt_ebitda": MetricResult(value=1.0),
        "cash_conversion": MetricResult(value=1.5),
        "net_income_margin": MetricResult(value=0.05),
    }
    inputs[invalid_metric] = MetricResult(value=None, invalid_reason=reason)
    result = evaluate_stage1_gates(**inputs, thresholds=DEFAULT_THRESHOLDS)
    assert result.status == "not_evaluable"
    assert result.flags == ()
    assert result.invalid_reasons == (reason,)


_HEALTHY = {
    "roic": MetricResult(value=0.15),
    "net_debt_ebitda": MetricResult(value=1.0),
    "cash_conversion": MetricResult(value=1.5),
    "net_income_margin": MetricResult(value=0.05),
}


def _hard_fail_result(metric_name):
    """A valid MetricResult inside metric_name's hard-fail zone, derived from DEFAULT_THRESHOLDS."""
    if metric_name == "roic":
        return MetricResult(value=DEFAULT_THRESHOLDS.roic.fail_below - 0.05)
    if metric_name == "net_debt_ebitda":
        return MetricResult(value=DEFAULT_THRESHOLDS.net_debt_ebitda.fail_at_or_above + 1.0)
    if metric_name == "cash_conversion":
        return MetricResult(value=DEFAULT_THRESHOLDS.cash_conversion.fail_below - 0.3)
    raise ValueError(metric_name)


@pytest.mark.parametrize("hard_fail_metric", ["roic", "net_debt_ebitda", "cash_conversion"])
def test_evaluate_stage1_gates_valid_hard_fail_wins_over_another_metrics_invalid_reason(hard_fail_metric):
    # Phase 2.0d, Entscheidung 1: once per metric that can trigger the precedence.
    invalid_metric = next(name for name in ("roic", "net_debt_ebitda", "cash_conversion") if name != hard_fail_metric)
    inputs = {
        **_HEALTHY,
        hard_fail_metric: _hard_fail_result(hard_fail_metric),
        invalid_metric: MetricResult(value=None, invalid_reason="some_guard"),
    }
    result = evaluate_stage1_gates(**inputs, thresholds=DEFAULT_THRESHOLDS)
    assert result.status == "failed"
    assert result.invalid_reasons == ("some_guard",)
    assert getattr(result, hard_fail_metric) == inputs[hard_fail_metric].value
    assert getattr(result, invalid_metric) is None


def test_evaluate_stage1_gates_hard_fail_exactly_at_the_net_debt_threshold_counts():
    # Boundary direction of Net Debt/EBITDA (at or above -> hard fail) carries over.
    result = evaluate_stage1_gates(
        **{
            **_HEALTHY,
            "net_debt_ebitda": MetricResult(value=DEFAULT_THRESHOLDS.net_debt_ebitda.fail_at_or_above),
            "cash_conversion": MetricResult(value=None, invalid_reason="ni_non_positive"),
        },
        thresholds=DEFAULT_THRESHOLDS,
    )
    assert result.status == "failed"


@pytest.mark.parametrize("invalid_metric", ["roic", "net_debt_ebitda", "cash_conversion"])
def test_evaluate_stage1_gates_invalid_metric_alone_is_never_a_hard_fail(invalid_metric):
    # Präzisierung 1: only a metric with its own invalid_reason == None counts. An
    # invalid metric has no value (MetricResult forbids both), so with the other
    # metrics healthy there is no valid hard fail and the company stays not_evaluable.
    inputs = {**_HEALTHY, invalid_metric: MetricResult(value=None, invalid_reason="net_debt_unreliable")}
    result = evaluate_stage1_gates(**inputs, thresholds=DEFAULT_THRESHOLDS)
    assert result.status == "not_evaluable"
    assert result.flags == ()
    assert getattr(result, invalid_metric) is None


def test_evaluate_stage1_gates_two_invalid_metrics_and_no_valid_hard_fail_stays_not_evaluable():
    result = evaluate_stage1_gates(
        **{
            **_HEALTHY,
            "roic": MetricResult(value=None, invalid_reason="net_debt_unreliable"),
            "net_debt_ebitda": MetricResult(value=None, invalid_reason="net_debt_unreliable"),
        },
        thresholds=DEFAULT_THRESHOLDS,
    )
    assert result.status == "not_evaluable"
    assert result.invalid_reasons == ("net_debt_unreliable", "net_debt_unreliable")


def test_evaluate_stage1_gates_grey_zone_next_to_an_invalid_metric_stays_not_evaluable():
    # The grey zone is not a hard fail: cash_conversion exactly at fail_below is a
    # flag, so an invalid ROIC still makes the company not_evaluable, flags empty.
    result = evaluate_stage1_gates(
        **{
            **_HEALTHY,
            "roic": MetricResult(value=None, invalid_reason="ic_non_positive"),
            "cash_conversion": MetricResult(value=DEFAULT_THRESHOLDS.cash_conversion.fail_below),
        },
        thresholds=DEFAULT_THRESHOLDS,
    )
    assert result.status == "not_evaluable"
    assert result.flags == ()


@pytest.mark.parametrize("invalid_metric", ["roic", "net_debt_ebitda", "cash_conversion"])
def test_evaluate_stage1_gates_margin_hard_fail_never_triggers_the_precedence(invalid_metric):
    # Präzisierung 2: only the margin is below its threshold and another metric is
    # invalid -> not_evaluable (Net-Income-Margin-Gate, Entscheidung 4, unchanged).
    inputs = {
        **_HEALTHY,
        "net_income_margin": MetricResult(value=DEFAULT_THRESHOLDS.net_income_margin.fail_below / 2),
        invalid_metric: MetricResult(value=None, invalid_reason="ni_non_positive"),
    }
    result = evaluate_stage1_gates(**inputs, thresholds=DEFAULT_THRESHOLDS)
    assert result.status == "not_evaluable"
    assert result.flags == ()
    assert result.invalid_reasons == ("ni_non_positive",)


def test_evaluate_stage1_gates_invalid_margin_next_to_a_valid_hard_fail_is_failed():
    # An invalid margin is "another metric with an invalid_reason" like any other.
    result = evaluate_stage1_gates(
        **{
            **_HEALTHY,
            "roic": _hard_fail_result("roic"),
            "net_income_margin": MetricResult(value=None, invalid_reason="revenue_non_positive"),
        },
        thresholds=DEFAULT_THRESHOLDS,
    )
    assert result.status == "failed"
    assert result.invalid_reasons == ("revenue_non_positive",)
    assert result.net_income_margin is None


def test_evaluate_stage1_gates_failed_with_invalid_reasons_keeps_reasons_raw_values_and_valid_flags():
    # Entscheidung 2: invalid_reasons in the fixed metric order, raw values None
    # where invalid, flags only from the valid metrics' grey zones.
    grey_cash_conversion = DEFAULT_THRESHOLDS.cash_conversion.fail_below  # grey zone, lower boundary
    result = evaluate_stage1_gates(
        roic=MetricResult(value=None, invalid_reason="ic_non_positive"),
        net_debt_ebitda=_hard_fail_result("net_debt_ebitda"),
        cash_conversion=MetricResult(value=grey_cash_conversion),
        net_income_margin=MetricResult(value=None, invalid_reason="revenue_non_positive"),
        thresholds=DEFAULT_THRESHOLDS,
        data_quality_flags=("pl_lags_other_statements",),
    )
    assert result.status == "failed"
    assert result.invalid_reasons == ("ic_non_positive", "revenue_non_positive")
    assert result.flags == ("cash_conversion",)
    assert result.roic is None
    assert result.net_debt_ebitda == _hard_fail_result("net_debt_ebitda").value
    assert result.cash_conversion == grey_cash_conversion
    assert result.net_income_margin is None
    assert result.data_quality_flags == ("pl_lags_other_statements",)


def test_evaluate_stage1_gates_margin_boundary_exactly_2_percent_passes():
    # EDGE30: net_income_margin exactly 2.0% every year - the >=2% threshold
    # is inclusive, so the exact boundary is a clean pass, not a failure.
    # Other three criteria are all clean pass too, isolating this boundary.
    result = _gate_result_for("EDGE30")
    assert result.status == "passed"
    assert result.flags == ()
    assert result.invalid_reasons == ()
    assert result.net_income_margin == pytest.approx(0.02, abs=0.0001)


def test_evaluate_stage1_gates_margin_below_2_percent_fails_despite_other_metrics_healthy():
    # THIN31: net_income_margin exactly 1.0% every year, but ROIC/Net
    # Debt/EBITDA/Cash Conversion are all clean pass. Demonstrates "hardest
    # single result wins" (Gates-Layer-Entscheidung 1): the company is
    # overall failed purely from the margin gate, and net_income_margin
    # never contributes to the flags list (it has no grey zone).
    result = _gate_result_for("THIN31")
    assert result.status == "failed"
    assert result.flags == ()
    assert result.invalid_reasons == ()
    assert result.net_income_margin == pytest.approx(0.01, abs=0.0001)


def test_evaluate_stage1_gates_margin_revenue_guard_isolated():
    # VOID32 isolates revenue_non_positive from the other three criteria
    # (all stay valid/healthy) - covered generically by the parametrized
    # not_evaluable test above; this asserts the isolation explicitly.
    result = _gate_result_for("VOID32")
    assert result.status == "not_evaluable"
    assert result.invalid_reasons == ("revenue_non_positive",)
    assert result.roic is not None
    assert result.net_debt_ebitda is not None
    assert result.cash_conversion is not None
    assert result.net_income_margin is None


def test_evaluate_stage1_gates_invalid_takes_precedence_over_margin_failure():
    # HOOL07: net_income<0 and cfo<0 in 2024 triggers the EXISTING
    # ni_non_positive guard on cash_conversion. Its net_income_margin 3yr
    # average (~0.57%) is well below the 2% threshold.
    #
    # Since docs/decisions.md "Phase 2.0d" (Entscheidung 1) this fixture is
    # "failed" - but because ROIC (~4.2%) and Net Debt/EBITDA (~5.7x) are valid
    # and in their own hard-fail zones, NOT because of the margin: the margin
    # is excluded from the precedence rule (Praezisierung 2). The regression
    # "a margin below 2% does not override ni_non_positive" is carried by the
    # _synthetic test below, whose other metrics are healthy.
    company = load_company("HOOL07")
    periods = company["periods"]
    margin_result = net_income_margin_3yr_average(periods)
    assert margin_result.invalid_reason is None
    assert margin_result.value < DEFAULT_THRESHOLDS.net_income_margin.fail_below  # would fail the margin gate in isolation

    result = _gate_result_for("HOOL07")
    assert result.status == "failed"
    assert result.invalid_reasons == ("ni_non_positive",)
    assert result.roic is not None and result.roic < DEFAULT_THRESHOLDS.roic.fail_below
    assert result.net_debt_ebitda is not None
    assert result.net_debt_ebitda >= DEFAULT_THRESHOLDS.net_debt_ebitda.fail_at_or_above
    assert result.cash_conversion is None

    # the margin is not needed for this verdict: with a healthy margin it stays failed
    without_margin_failure = evaluate_stage1_gates(
        roic=roic_3yr_average(periods, thresholds=DEFAULT_THRESHOLDS.roic),
        net_debt_ebitda=net_debt_ebitda_latest(periods),
        cash_conversion=cash_conversion_3yr_average(periods),
        net_income_margin=MetricResult(value=0.05),
        thresholds=DEFAULT_THRESHOLDS,
    )
    assert without_margin_failure.status == "failed"


def test_evaluate_stage1_gates_invalid_takes_precedence_over_margin_failure_synthetic():
    # Direct-input regression guard (mirrors tests/test_db.py's synthetic
    # gate_results pattern) for the exact scenario named in the task: NI<=0
    # and margin<2% in the same year. Uses synthetic MetricResults instead
    # of a fixture so the scenario is deterministic and doesn't depend on a
    # fixture's arithmetic happening to line up.
    result = evaluate_stage1_gates(
        roic=MetricResult(value=0.15),
        net_debt_ebitda=MetricResult(value=1.0),
        cash_conversion=MetricResult(value=None, invalid_reason="ni_non_positive"),
        net_income_margin=MetricResult(value=0.01),
        thresholds=DEFAULT_THRESHOLDS,
    )
    assert result.status == "not_evaluable"
    assert result.status != "failed"
    assert result.invalid_reasons == ("ni_non_positive",)


def test_rank_companies_groups_by_flag_count_then_ticker():
    gate_results = {
        company_id: _gate_result_for(company_id)
        for company_id in ("ACME01", "APRT11", "SPCL20", "BLKW13", "GEKO12", "NAKA17")
    }

    ranked = rank_companies(gate_results)

    assert [ticker for ticker, _ in ranked] == ["ACME01", "APRT11", "SPCL20", "BLKW13"]
    assert [len(result.flags) for _, result in ranked] == [0, 1, 1, 2]

    ranked_tickers = {ticker for ticker, _ in ranked}
    assert "GEKO12" not in ranked_tickers
    assert "NAKA17" not in ranked_tickers


def test_collect_data_quality_flags_is_distinct_sorted_and_company_level():
    periods = [
        {"fiscal_year": 2023, "data_quality_flags": ["pl_lags_other_statements", "debt_component_missing_defaulted_zero"]},
        {"fiscal_year": 2024, "data_quality_flags": ["debt_component_missing_defaulted_zero"]},
        {"fiscal_year": 2025, "data_quality_flags": []},
    ]
    assert collect_data_quality_flags(periods) == (
        "debt_component_missing_defaulted_zero",
        "pl_lags_other_statements",
    )


def test_collect_data_quality_flags_tolerates_missing_key_null_and_no_periods():
    # the dummy fixtures carry no data_quality_flags key at all
    assert collect_data_quality_flags(load_company("ACME01")["periods"]) == ()
    assert collect_data_quality_flags([{"fiscal_year": 2024, "data_quality_flags": None}]) == ()
    assert collect_data_quality_flags([]) == ()


def test_evaluate_stage1_gates_passes_data_quality_flags_through_without_affecting_the_verdict():
    inputs = dict(
        roic=MetricResult(value=0.15),
        net_debt_ebitda=MetricResult(value=1.0),
        cash_conversion=MetricResult(value=1.5),
        net_income_margin=MetricResult(value=0.05),
        thresholds=DEFAULT_THRESHOLDS,
    )
    plain = evaluate_stage1_gates(**inputs)
    flagged = evaluate_stage1_gates(**inputs, data_quality_flags=("pl_lags_other_statements",))
    assert plain.data_quality_flags == ()
    assert flagged.data_quality_flags == ("pl_lags_other_statements",)
    assert (flagged.status, flagged.flags, flagged.invalid_reasons) == (plain.status, plain.flags, plain.invalid_reasons)

    # also carried on a not_evaluable result, and stored as a tuple even if a list is passed
    not_evaluable = evaluate_stage1_gates(
        **{**inputs, "roic": MetricResult(value=None, invalid_reason="ic_non_positive")},
        data_quality_flags=["year_dropped_missing_required_field"],
    )
    assert not_evaluable.status == "not_evaluable"
    assert not_evaluable.data_quality_flags == ("year_dropped_missing_required_field",)
    hash(not_evaluable)  # still hashable: a tuple, not a list


# --- business-model profile fields (Issue #51, docs/decisions.md "Phase 2.0e") -------


def test_unsupported_business_model_result_carries_the_profile_and_no_values_or_reasons():
    result = unsupported_business_model_result("banks", data_quality_flags=("x",))
    assert result.status == UNSUPPORTED_BUSINESS_MODEL == "unsupported_business_model"
    assert result.flags == ()
    assert result.invalid_reasons == ()  # not a data gap, so "invalid_reasons non-empty" keeps meaning one
    assert (result.roic, result.net_debt_ebitda, result.cash_conversion, result.net_income_margin) == (None,) * 4
    assert result.profile == "banks"
    assert result.borderline_sector is False
    assert result.data_quality_flags == ("x",)
    hash(result)


def test_profile_fields_default_to_none_meaning_not_checked():
    result = _gate_result_for("ACME01")
    assert result.profile is None
    assert result.borderline_sector is None


@pytest.mark.parametrize("company_id", ["ACME01", "APRT11", "GEKO12", "ORBT18"])
def test_profile_and_borderline_never_influence_status_flags_or_invalid_reasons(company_id):
    # passed / passed_with_flags / failed / not_evaluable, each with and without the marker
    plain = _gate_result_for(company_id)
    for borderline in (False, True):
        marked = dataclasses.replace(plain, profile="non_financial", borderline_sector=borderline)
        assert (marked.status, marked.flags, marked.invalid_reasons) == (plain.status, plain.flags, plain.invalid_reasons)
        assert (marked.roic, marked.net_debt_ebitda, marked.cash_conversion, marked.net_income_margin) == (
            plain.roic,
            plain.net_debt_ebitda,
            plain.cash_conversion,
            plain.net_income_margin,
        )


def test_a_borderline_marker_does_not_turn_passed_into_passed_with_flags_or_change_the_rank():
    gate_results = {company_id: _gate_result_for(company_id) for company_id in ("ACME01", "APRT11", "SPCL20", "BLKW13")}
    marked = {
        ticker: dataclasses.replace(result, profile="non_financial", borderline_sector=ticker == "ACME01")
        for ticker, result in gate_results.items()
    }
    assert marked["ACME01"].status == "passed" and marked["ACME01"].flags == ()
    assert [ticker for ticker, _ in rank_companies(marked)] == [ticker for ticker, _ in rank_companies(gate_results)]


def test_unsupported_business_model_is_not_ranked():
    gate_results = {"ACME01": _gate_result_for("ACME01"), "BANK01": unsupported_business_model_result("banks")}
    assert [ticker for ticker, _ in rank_companies(gate_results)] == ["ACME01"]

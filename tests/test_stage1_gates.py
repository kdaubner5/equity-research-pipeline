import pytest

from conftest import load_company
from metrics_layer.cash_conversion import cash_conversion_3yr_average, net_income_margin_3yr_average
from metrics_layer.net_debt_ebitda import net_debt_ebitda_latest
from metrics_layer.result import MetricResult
from metrics_layer.roic import roic_3yr_average
from gates_layer.stage1_gates import collect_data_quality_flags, evaluate_stage1_gates, rank_companies


def _gate_result_for(company_id):
    company = load_company(company_id)
    periods = company["periods"]
    return evaluate_stage1_gates(
        roic=roic_3yr_average(periods),
        net_debt_ebitda=net_debt_ebitda_latest(periods),
        cash_conversion=cash_conversion_3yr_average(periods),
        net_income_margin=net_income_margin_3yr_average(periods),
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
        ("NAKA17", "ic_non_positive"),
        ("ORBT18", "insufficient_years"),
        ("WEYL19", "ebitda_non_positive"),
        ("MDYN06", "ni_non_positive"),
        ("MURK26", "net_debt_unreliable"),
        ("VOID32", "revenue_non_positive"),
    ],
)
def test_evaluate_stage1_gates_not_evaluable(company_id, expected_reason):
    result = _gate_result_for(company_id)
    assert result.status == "not_evaluable"
    assert result.flags == ()
    assert expected_reason in result.invalid_reasons


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
    # average (~0.57%) is itself well below the new 2% threshold - would
    # be "failed" in isolation - but the company must stay "not_evaluable",
    # never "failed", since invalid_reason still takes precedence over any
    # failed/passed classification (Gates-Layer-Entscheidung 2, unchanged).
    company = load_company("HOOL07")
    periods = company["periods"]
    margin_result = net_income_margin_3yr_average(periods)
    assert margin_result.invalid_reason is None
    assert margin_result.value < 0.02  # would fail the margin gate in isolation

    result = _gate_result_for("HOOL07")
    assert result.status == "not_evaluable"
    assert result.status != "failed"
    assert result.invalid_reasons == ("ni_non_positive",)


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

import pytest

from conftest import load_company
from metrics_layer.cash_conversion import cash_conversion_3yr_average
from metrics_layer.net_debt_ebitda import net_debt_ebitda_latest
from metrics_layer.roic import roic_3yr_average
from gates_layer.stage1_gates import evaluate_stage1_gates, rank_companies


def _gate_result_for(company_id):
    company = load_company(company_id)
    periods = company["periods"]
    return evaluate_stage1_gates(
        roic=roic_3yr_average(periods),
        net_debt_ebitda=net_debt_ebitda_latest(periods),
        cash_conversion=cash_conversion_3yr_average(periods),
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
    ],
)
def test_evaluate_stage1_gates_not_evaluable(company_id, expected_reason):
    result = _gate_result_for(company_id)
    assert result.status == "not_evaluable"
    assert result.flags == ()
    assert expected_reason in result.invalid_reasons


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

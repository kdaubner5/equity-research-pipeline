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
    assert result.flags == []
    assert result.invalid_reasons == []
    assert result.roic == pytest.approx(0.1367, abs=0.001)
    assert result.net_debt_ebitda == pytest.approx(800 / 790, abs=0.001)
    assert result.cash_conversion == pytest.approx(1.6386, abs=0.001)


@pytest.mark.parametrize(
    "company_id,expected_flags",
    [
        ("APRT11", ["roic"]),
        ("SPCL20", ["cash_conversion"]),
    ],
)
def test_evaluate_stage1_gates_single_flag(company_id, expected_flags):
    result = _gate_result_for(company_id)
    assert result.status == "passed_with_flags"
    assert result.flags == expected_flags
    assert result.invalid_reasons == []


def test_evaluate_stage1_gates_two_flags_simultaneously():
    result = _gate_result_for("BLKW13")
    assert result.status == "passed_with_flags"
    assert result.flags == ["roic", "net_debt_ebitda"]
    assert result.invalid_reasons == []
    assert result.roic == pytest.approx(0.1100, abs=0.001)
    assert result.net_debt_ebitda == pytest.approx(2.40, abs=0.001)
    assert result.cash_conversion == pytest.approx(1.43, abs=0.01)


def test_evaluate_stage1_gates_failed():
    result = _gate_result_for("GEKO12")
    assert result.status == "failed"
    assert result.invalid_reasons == []
    assert result.net_debt_ebitda == pytest.approx(2.50, abs=0.001)
    # the fixture's ROIC and Cash Conversion are both clean pass in
    # isolation, so the failed status is driven purely by Net Debt/EBITDA
    assert result.flags == []


@pytest.mark.parametrize(
    "company_id,expected_reason",
    [
        ("NAKA17", "ic_non_positive"),
        ("ORBT18", "insufficient_years"),
        ("WEYL19", "ebitda_non_positive"),
        ("MDYN06", "ni_non_positive"),
    ],
)
def test_evaluate_stage1_gates_not_evaluable(company_id, expected_reason):
    result = _gate_result_for(company_id)
    assert result.status == "not_evaluable"
    assert result.flags == []
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

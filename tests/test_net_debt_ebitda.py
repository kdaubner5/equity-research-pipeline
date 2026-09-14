import pytest

from conftest import load_company
from metrics_layer.result import MetricInputError
from metrics_layer.net_debt_ebitda import net_debt_ebitda_for_period, net_debt_ebitda_latest


def test_net_debt_ebitda_normal_case():
    company = load_company("ACME01")
    result = net_debt_ebitda_latest(company["periods"])
    assert result.invalid_reason is None
    assert result.value == pytest.approx(800 / 790, abs=0.001)


@pytest.mark.parametrize(
    "company_id,expected",
    [
        ("VULC14", 2.30),  # untere Grauzonen-Bandgrenze
        ("BLKW13", 2.40),  # innerhalb der Grauzone
        ("GEKO12", 2.50),  # harte Schwelle selbst
        ("CYBR15", 2.60),  # klar oberhalb der Schwelle
    ],
)
def test_net_debt_ebitda_threshold_fixtures(company_id, expected):
    company = load_company(company_id)
    result = net_debt_ebitda_latest(company["periods"])
    assert result.invalid_reason is None
    assert result.value == pytest.approx(expected, abs=0.001)


def test_net_debt_ebitda_non_positive_guard():
    company = load_company("WEYL19")
    latest = next(p for p in company["periods"] if p["fiscal_year"] == 2024)
    assert latest["ebitda"] <= 0

    result = net_debt_ebitda_latest(company["periods"])
    assert result.value is None
    assert result.invalid_reason == "ebitda_non_positive"

    healthy = next(p for p in company["periods"] if p["fiscal_year"] == 2022)
    healthy_result = net_debt_ebitda_for_period(net_debt=healthy["net_debt"], ebitda=healthy["ebitda"])
    assert healthy_result.invalid_reason is None
    assert healthy_result.value == pytest.approx(1400 / 520, abs=0.001)


def test_net_debt_ebitda_empty_periods_flags_no_periods():
    result = net_debt_ebitda_latest([])
    assert result.value is None
    assert result.invalid_reason == "no_periods"


def test_net_debt_ebitda_duplicate_fiscal_year_raises_caller_error():
    company = load_company("ACME01")
    duplicated_periods = company["periods"] + [company["periods"][-1]]

    with pytest.raises(MetricInputError):
        net_debt_ebitda_latest(duplicated_periods)


def test_net_debt_ebitda_point_in_time_not_averaged():
    company = load_company("TYRL16")
    result = net_debt_ebitda_latest(company["periods"])
    assert result.invalid_reason is None

    # correct: only the latest year (2024) counts -> clearly healthy
    assert result.value == pytest.approx(1.0, abs=0.001)

    # a wrongly averaging implementation would compute (3.5+3.0+1.0)/3 = 2.5
    # (hard-fail territory) instead - make sure we are nowhere near that
    wrong_average = 2.5
    assert result.value != pytest.approx(wrong_average, abs=0.001)

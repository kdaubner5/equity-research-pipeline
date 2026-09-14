import pytest

from conftest import load_company
from metrics_layer.result import MetricInputError
from metrics_layer.roic import roic_for_period, roic_3yr_average


def test_roic_normal_case():
    company = load_company("ACME01")
    result = roic_3yr_average(company["periods"])
    assert result.invalid_reason is None
    assert result.value == pytest.approx(0.1367, abs=0.001)


@pytest.mark.parametrize(
    "company_id,expected",
    [
        ("APRT11", 0.1107),
        ("BLKW13", 0.1100),
    ],
)
def test_roic_grey_zone(company_id, expected):
    company = load_company(company_id)
    result = roic_3yr_average(company["periods"])
    assert result.invalid_reason is None
    assert result.value == pytest.approx(expected, abs=0.001)
    assert 0.10 <= result.value <= 0.12


def test_roic_invested_capital_guard_flags_whole_company():
    company = load_company("NAKA17")
    result = roic_3yr_average(company["periods"])
    assert result.value is None
    assert result.invalid_reason == "ic_non_positive"

    healthy_years = [p for p in company["periods"] if p["fiscal_year"] != 2024]
    naive_average_of_healthy_years = sum(
        roic_for_period(**{k: p[k] for k in ("ebit", "tax_rate", "equity", "net_debt")}).value
        for p in healthy_years
    ) / len(healthy_years)
    assert naive_average_of_healthy_years == pytest.approx(0.1364, abs=0.001)
    assert result.value != naive_average_of_healthy_years


def test_roic_fewer_than_3_periods_flags_invalid():
    company = load_company("ORBT18")
    assert len(company["periods"]) == 2

    result = roic_3yr_average(company["periods"])
    assert result.value is None
    assert result.invalid_reason == "insufficient_years"

    naive_average_of_2_years = sum(
        roic_for_period(**{k: p[k] for k in ("ebit", "tax_rate", "equity", "net_debt")}).value
        for p in company["periods"]
    ) / len(company["periods"])
    assert result.value != naive_average_of_2_years


def test_roic_more_than_3_periods_raises_caller_error():
    company = load_company("ACME01")
    duplicated_periods = company["periods"] + [company["periods"][0]]
    assert len(duplicated_periods) == 4

    with pytest.raises(MetricInputError):
        roic_3yr_average(duplicated_periods)


def test_roic_duplicate_fiscal_year_within_3_periods_raises_caller_error():
    company = load_company("ACME01")
    periods = company["periods"]
    duplicated_same_count = [periods[0], periods[0], periods[1]]
    assert len(duplicated_same_count) == 3

    with pytest.raises(MetricInputError):
        roic_3yr_average(duplicated_same_count)


def test_roic_for_period_guard_on_single_bad_year():
    company = load_company("NAKA17")
    periods_by_year = {p["fiscal_year"]: p for p in company["periods"]}

    healthy = periods_by_year[2022]
    healthy_result = roic_for_period(
        ebit=healthy["ebit"],
        tax_rate=healthy["tax_rate"],
        equity=healthy["equity"],
        net_debt=healthy["net_debt"],
    )
    assert healthy_result.invalid_reason is None
    assert healthy_result.value == pytest.approx(0.1357, abs=0.001)

    bad_year = periods_by_year[2024]
    assert bad_year["equity"] + bad_year["net_debt"] <= 0
    bad_result = roic_for_period(
        ebit=bad_year["ebit"],
        tax_rate=bad_year["tax_rate"],
        equity=bad_year["equity"],
        net_debt=bad_year["net_debt"],
    )
    assert bad_result.value is None
    assert bad_result.invalid_reason == "ic_non_positive"


def test_roic_different_invalid_reasons_are_distinguishable():
    ic_guard_result = roic_3yr_average(load_company("NAKA17")["periods"])
    insufficient_years_result = roic_3yr_average(load_company("ORBT18")["periods"])

    assert ic_guard_result.invalid_reason == "ic_non_positive"
    assert insufficient_years_result.invalid_reason == "insufficient_years"
    assert ic_guard_result.invalid_reason != insufficient_years_result.invalid_reason

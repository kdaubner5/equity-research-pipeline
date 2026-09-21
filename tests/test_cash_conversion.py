import pytest

from conftest import load_company
from metrics_layer.result import MetricInputError
from metrics_layer.cash_conversion import (
    cash_conversion_for_period,
    cash_conversion_3yr_average,
    net_income_margin_for_period,
    net_income_margin_3yr_average,
)


def test_cash_conversion_normal_case():
    company = load_company("ACME01")
    result = cash_conversion_3yr_average(company["periods"])
    assert result.invalid_reason is None
    assert result.value == pytest.approx(1.6386, abs=0.001)


def test_cash_conversion_grey_zone():
    company = load_company("SPCL20")
    result = cash_conversion_3yr_average(company["periods"])
    assert result.invalid_reason is None
    assert result.value == pytest.approx(0.90, abs=0.001)
    assert 0.8 <= result.value <= 1.0


@pytest.mark.parametrize(
    "company_id,expected",
    [
        ("OSCP21", 0.80),  # untere Grauzonen-Grenze: 0.8 <= x, also Flag, NICHT durchgefallen
        ("DUFF22", 1.00),  # obere Grauzonen-Grenze: x >= 1.0, also sauber bestanden, NICHT Grauzone
    ],
)
def test_cash_conversion_grey_zone_boundaries(company_id, expected):
    company = load_company(company_id)
    result = cash_conversion_3yr_average(company["periods"])
    assert result.invalid_reason is None
    assert result.value == pytest.approx(expected, abs=0.001)


def test_cash_conversion_ni_guard_flags_whole_company():
    company = load_company("MDYN06")
    result = cash_conversion_3yr_average(company["periods"])
    assert result.value is None
    assert result.invalid_reason == "ni_non_positive"

    healthy_years = [p for p in company["periods"] if p["fiscal_year"] != 2024]
    naive_average_of_healthy_years = sum(
        cash_conversion_for_period(cfo=p["cfo"], net_income=p["net_income"]).value
        for p in healthy_years
    ) / len(healthy_years)
    assert naive_average_of_healthy_years == pytest.approx(2.9722, abs=0.001)
    assert result.value != naive_average_of_healthy_years


def test_cash_conversion_for_period_guard_on_single_bad_year():
    company = load_company("MDYN06")
    periods_by_year = {p["fiscal_year"]: p for p in company["periods"]}

    healthy = periods_by_year[2022]
    healthy_result = cash_conversion_for_period(cfo=healthy["cfo"], net_income=healthy["net_income"])
    assert healthy_result.invalid_reason is None
    assert healthy_result.value == pytest.approx(2.2778, abs=0.001)

    bad_year = periods_by_year[2024]
    assert bad_year["net_income"] <= 0
    bad_result = cash_conversion_for_period(cfo=bad_year["cfo"], net_income=bad_year["net_income"])
    assert bad_result.value is None
    assert bad_result.invalid_reason == "ni_non_positive"


@pytest.mark.parametrize(
    "company_id",
    [
        "MDYN06",  # net_income == 0 (2024)
        "HOOL07",  # net_income < 0 and cfo < 0 (2024)
        "WONK08",  # net_income < 0, cfo > 0 (2024)
    ],
)
def test_cash_conversion_guard_fixtures(company_id):
    company = load_company(company_id)
    result = cash_conversion_3yr_average(company["periods"])
    assert result.value is None
    assert result.invalid_reason == "ni_non_positive"


def test_cash_conversion_fewer_than_3_periods_flags_invalid():
    company = load_company("ORBT18")
    assert len(company["periods"]) == 2

    result = cash_conversion_3yr_average(company["periods"])
    assert result.value is None
    assert result.invalid_reason == "insufficient_years"

    naive_average_of_2_years = sum(
        cash_conversion_for_period(cfo=p["cfo"], net_income=p["net_income"]).value
        for p in company["periods"]
    ) / len(company["periods"])
    assert result.value != naive_average_of_2_years


def test_cash_conversion_more_than_3_periods_raises_caller_error():
    company = load_company("ACME01")
    duplicated_periods = company["periods"] + [company["periods"][0]]
    assert len(duplicated_periods) == 4

    with pytest.raises(MetricInputError):
        cash_conversion_3yr_average(duplicated_periods)


def test_cash_conversion_duplicate_fiscal_year_within_3_periods_raises_caller_error():
    company = load_company("ACME01")
    periods = company["periods"]
    duplicated_same_count = [periods[0], periods[0], periods[1]]
    assert len(duplicated_same_count) == 3

    with pytest.raises(MetricInputError):
        cash_conversion_3yr_average(duplicated_same_count)


def test_cash_conversion_duplicate_fiscal_year_below_3_periods_raises_caller_error():
    company = load_company("ACME01")
    periods = company["periods"]
    duplicated_2_periods = [periods[0], periods[0]]
    assert len(duplicated_2_periods) == 2

    # a duplicate is a caller error at any period count - it must not be
    # silently accepted as "insufficient_years" (2 distinct years) instead
    with pytest.raises(MetricInputError):
        cash_conversion_3yr_average(duplicated_2_periods)


def test_cash_conversion_different_invalid_reasons_are_distinguishable():
    ni_guard_result = cash_conversion_3yr_average(load_company("MDYN06")["periods"])
    insufficient_years_result = cash_conversion_3yr_average(load_company("ORBT18")["periods"])

    assert ni_guard_result.invalid_reason == "ni_non_positive"
    assert insufficient_years_result.invalid_reason == "insufficient_years"
    assert ni_guard_result.invalid_reason != insufficient_years_result.invalid_reason


def test_net_income_margin_normal_case():
    company = load_company("ACME01")
    result = net_income_margin_3yr_average(company["periods"])
    assert result.invalid_reason is None
    assert result.value == pytest.approx(0.0827, abs=0.001)


def test_net_income_margin_boundary_exactly_2_percent_is_not_failed():
    # EDGE30: every year exactly 2.0% (analog zu INGN23/SIRI24 - jedes Jahr
    # exakt an der Schwelle) - the >= 2% threshold is inclusive, so the
    # exact boundary itself is NOT a failure. cash_conversion.py doesn't
    # classify (no grey zone concept here either) - Gates-Layer does that.
    company = load_company("EDGE30")
    result = net_income_margin_3yr_average(company["periods"])
    assert result.invalid_reason is None
    assert result.value == pytest.approx(0.02, abs=0.0001)


def test_net_income_margin_clearly_below_2_percent():
    company = load_company("THIN31")
    result = net_income_margin_3yr_average(company["periods"])
    assert result.invalid_reason is None
    assert result.value == pytest.approx(0.01, abs=0.0001)


def test_net_income_margin_revenue_guard_flags_whole_company():
    # VOID32: 2022/2023 healthy (~8% margin); 2024 revenue=0 - net_income
    # stays positive that year (10), isolating this guard from the
    # existing ni_non_positive guard on cash_conversion (different metric).
    company = load_company("VOID32")
    result = net_income_margin_3yr_average(company["periods"])
    assert result.value is None
    assert result.invalid_reason == "revenue_non_positive"

    healthy_years = [p for p in company["periods"] if p["fiscal_year"] != 2024]
    naive_average_of_healthy_years = sum(
        net_income_margin_for_period(net_income=p["net_income"], revenue=p["revenue"]).value
        for p in healthy_years
    ) / len(healthy_years)
    assert naive_average_of_healthy_years == pytest.approx(0.08, abs=0.001)
    assert result.value != naive_average_of_healthy_years


def test_net_income_margin_for_period_guard_on_single_bad_year():
    company = load_company("VOID32")
    periods_by_year = {p["fiscal_year"]: p for p in company["periods"]}

    healthy = periods_by_year[2022]
    healthy_result = net_income_margin_for_period(net_income=healthy["net_income"], revenue=healthy["revenue"])
    assert healthy_result.invalid_reason is None
    assert healthy_result.value == pytest.approx(0.08, abs=0.001)

    bad_year = periods_by_year[2024]
    assert bad_year["revenue"] <= 0
    bad_result = net_income_margin_for_period(net_income=bad_year["net_income"], revenue=bad_year["revenue"])
    assert bad_result.value is None
    assert bad_result.invalid_reason == "revenue_non_positive"


def test_net_income_margin_for_period_negative_net_income_is_valid_not_invalid():
    # Explicit design choice (docs/decisions.md, Entscheidung 2): no guard
    # on net_income's sign - a negative margin against positive revenue is
    # a valid (very poor) result, not an undefined one. This is exactly the
    # case the Gates-Layer's < 2% threshold exists to catch as "failed".
    result = net_income_margin_for_period(net_income=-100, revenue=2000)
    assert result.invalid_reason is None
    assert result.value == pytest.approx(-0.05, abs=0.0001)


def test_net_income_margin_fewer_than_3_periods_flags_invalid():
    company = load_company("ORBT18")
    assert len(company["periods"]) == 2

    result = net_income_margin_3yr_average(company["periods"])
    assert result.value is None
    assert result.invalid_reason == "insufficient_years"

    naive_average_of_2_years = sum(
        net_income_margin_for_period(net_income=p["net_income"], revenue=p["revenue"]).value
        for p in company["periods"]
    ) / len(company["periods"])
    assert result.value != naive_average_of_2_years


def test_net_income_margin_more_than_3_periods_raises_caller_error():
    company = load_company("ACME01")
    duplicated_periods = company["periods"] + [company["periods"][0]]
    assert len(duplicated_periods) == 4

    with pytest.raises(MetricInputError):
        net_income_margin_3yr_average(duplicated_periods)


def test_net_income_margin_duplicate_fiscal_year_within_3_periods_raises_caller_error():
    company = load_company("ACME01")
    periods = company["periods"]
    duplicated_same_count = [periods[0], periods[0], periods[1]]
    assert len(duplicated_same_count) == 3

    with pytest.raises(MetricInputError):
        net_income_margin_3yr_average(duplicated_same_count)


def test_net_income_margin_duplicate_fiscal_year_below_3_periods_raises_caller_error():
    company = load_company("ACME01")
    periods = company["periods"]
    duplicated_2_periods = [periods[0], periods[0]]
    assert len(duplicated_2_periods) == 2

    with pytest.raises(MetricInputError):
        net_income_margin_3yr_average(duplicated_2_periods)


def test_net_income_margin_different_invalid_reasons_are_distinguishable():
    revenue_guard_result = net_income_margin_3yr_average(load_company("VOID32")["periods"])
    insufficient_years_result = net_income_margin_3yr_average(load_company("ORBT18")["periods"])

    assert revenue_guard_result.invalid_reason == "revenue_non_positive"
    assert insufficient_years_result.invalid_reason == "insufficient_years"
    assert revenue_guard_result.invalid_reason != insufficient_years_result.invalid_reason

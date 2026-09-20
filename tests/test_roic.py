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
        ("INGN23", 0.1200),
        ("SIRI24", 0.1000),
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


def test_roic_duplicate_fiscal_year_below_3_periods_raises_caller_error():
    company = load_company("ACME01")
    periods = company["periods"]
    duplicated_2_periods = [periods[0], periods[0]]
    assert len(duplicated_2_periods) == 2

    # a duplicate is a caller error at any period count - it must not be
    # silently accepted as "insufficient_years" (2 distinct years) instead
    with pytest.raises(MetricInputError):
        roic_3yr_average(duplicated_2_periods)


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
    undefined_tax_rate_result = roic_3yr_average(load_company("DHRM25")["periods"])

    reasons = {
        ic_guard_result.invalid_reason,
        insufficient_years_result.invalid_reason,
        undefined_tax_rate_result.invalid_reason,
    }
    assert reasons == {"ic_non_positive", "insufficient_years", "undefined_tax_rate"}


def test_roic_net_debt_unreliable_guard_flags_whole_company():
    # MURK26: net_debt=None in 2024, which is both the last of the 3 ROIC
    # years and (separately) the Net-Debt/EBITDA stichtag year - see
    # test_net_debt_ebitda.py for the matching Net-Debt/EBITDA-side test on
    # the same fixture.
    company = load_company("MURK26")
    result = roic_3yr_average(company["periods"])
    assert result.value is None
    assert result.invalid_reason == "net_debt_unreliable"

    healthy_years = [p for p in company["periods"] if p["fiscal_year"] != 2024]
    naive_average_of_healthy_years = sum(
        roic_for_period(**{k: p[k] for k in ("ebit", "tax_rate", "equity", "net_debt")}).value
        for p in healthy_years
    ) / len(healthy_years)
    assert naive_average_of_healthy_years == pytest.approx(0.1347, abs=0.001)
    assert result.value != naive_average_of_healthy_years


def test_roic_net_debt_unreliable_flags_whole_company_even_in_an_older_year():
    # FOGG27: net_debt=None in 2022 (the OLDEST of the 3 years, not the
    # Net-Debt/EBITDA stichtag) - unlike Net-Debt/EBITDA (a point-in-time
    # metric that only looks at the latest year, see
    # test_net_debt_ebitda_unaffected_by_unreliable_net_debt_in_an_older_year
    # in test_net_debt_ebitda.py), ROIC's 3-year average is flagged by an
    # unreliable net_debt in ANY of its 3 years, regardless of position -
    # same "one bad year disqualifies the whole average" rule as
    # ic_non_positive (NAKA17) and undefined_tax_rate (DHRM25).
    company = load_company("FOGG27")
    result = roic_3yr_average(company["periods"])
    assert result.value is None
    assert result.invalid_reason == "net_debt_unreliable"

    healthy_years = [p for p in company["periods"] if p["fiscal_year"] != 2022]
    naive_average_of_healthy_years = sum(
        roic_for_period(**{k: p[k] for k in ("ebit", "tax_rate", "equity", "net_debt")}).value
        for p in healthy_years
    ) / len(healthy_years)
    assert naive_average_of_healthy_years == pytest.approx(0.1361, abs=0.001)
    assert result.value != naive_average_of_healthy_years


def test_roic_for_period_guard_on_net_debt_none():
    company = load_company("MURK26")
    periods_by_year = {p["fiscal_year"]: p for p in company["periods"]}

    healthy = periods_by_year[2022]
    healthy_result = roic_for_period(
        ebit=healthy["ebit"],
        tax_rate=healthy["tax_rate"],
        equity=healthy["equity"],
        net_debt=healthy["net_debt"],
    )
    assert healthy_result.invalid_reason is None
    assert healthy_result.value == pytest.approx(0.1339, abs=0.001)

    bad_year = periods_by_year[2024]
    assert bad_year["net_debt"] is None
    bad_result = roic_for_period(
        ebit=bad_year["ebit"],
        tax_rate=bad_year["tax_rate"],
        equity=bad_year["equity"],
        net_debt=bad_year["net_debt"],
    )
    assert bad_result.value is None
    assert bad_result.invalid_reason == "net_debt_unreliable"


def test_roic_normal_net_debt_is_not_treated_as_unreliable():
    # Regression guard for the net_debt_unreliable check itself: an
    # ordinary numeric net_debt (the "under 10%" informational-flag case
    # per docs/decisions.md, where a data_layer adapter keeps the real
    # value instead of substituting None) must compute exactly as before -
    # the new guard must not misfire on a real number.
    result = roic_for_period(ebit=500, tax_rate=0.25, equity=2000, net_debt=800)
    assert result.invalid_reason is None
    assert result.value == pytest.approx(0.1339, abs=0.001)


def test_roic_for_period_undefined_tax_rate_takes_precedence_over_ic_guard():
    # Pins down guard ordering for a period where both conditions hold at
    # once (undefined tax_rate and invested_capital <= 0) - per
    # docs/decisions.md, the tax_rate check runs first (it guards the
    # NOPAT numerator, checked before the IC denominator), so
    # "undefined_tax_rate" wins over "ic_non_positive". A future reordering
    # of the two guards would silently flip which reason is reported here.
    result = roic_for_period(ebit=100, tax_rate=None, equity=-500, net_debt=200)
    assert result.value is None
    assert result.invalid_reason == "undefined_tax_rate"


def test_roic_undefined_tax_rate_guard_flags_whole_company():
    company = load_company("DHRM25")
    result = roic_3yr_average(company["periods"])
    assert result.value is None
    assert result.invalid_reason == "undefined_tax_rate"

    healthy_years = [p for p in company["periods"] if p["fiscal_year"] != 2024]
    naive_average_of_healthy_years = sum(
        roic_for_period(**{k: p[k] for k in ("ebit", "tax_rate", "equity", "net_debt")}).value
        for p in healthy_years
    ) / len(healthy_years)
    assert naive_average_of_healthy_years == pytest.approx(0.125, abs=0.001)
    assert result.value != naive_average_of_healthy_years


def test_roic_for_period_guard_on_undefined_tax_rate():
    company = load_company("DHRM25")
    periods_by_year = {p["fiscal_year"]: p for p in company["periods"]}

    healthy = periods_by_year[2022]
    healthy_result = roic_for_period(
        ebit=healthy["ebit"],
        tax_rate=healthy["tax_rate"],
        equity=healthy["equity"],
        net_debt=healthy["net_debt"],
    )
    assert healthy_result.invalid_reason is None
    assert healthy_result.value == pytest.approx(0.125, abs=0.001)

    bad_year = periods_by_year[2024]
    assert bad_year["tax_rate"] is None
    bad_result = roic_for_period(
        ebit=bad_year["ebit"],
        tax_rate=bad_year["tax_rate"],
        equity=bad_year["equity"],
        net_debt=bad_year["net_debt"],
    )
    assert bad_result.value is None
    assert bad_result.invalid_reason == "undefined_tax_rate"

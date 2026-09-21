import pytest

from conftest import load_company
from metrics_layer.result import MetricInputError
from metrics_layer.roic import roic_for_period, roic_3yr_average, IC_ASSET_FLOOR_RATIO


def test_roic_normal_case():
    company = load_company("ACME01")
    result = roic_3yr_average(company["periods"])
    assert result.invalid_reason is None
    assert result.value == pytest.approx(0.1343, abs=0.001)


@pytest.mark.parametrize(
    "company_id,expected",
    [
        ("APRT11", 0.1102),
        ("BLKW13", 0.1095),
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
        roic_for_period(**{k: p[k] for k in ("ebit", "equity", "net_debt", "total_assets")}).value
        for p in healthy_years
    ) / len(healthy_years)
    assert naive_average_of_healthy_years == pytest.approx(0.1347, abs=0.001)
    assert result.value != naive_average_of_healthy_years


def test_roic_fewer_than_3_periods_flags_invalid():
    company = load_company("ORBT18")
    assert len(company["periods"]) == 2

    result = roic_3yr_average(company["periods"])
    assert result.value is None
    assert result.invalid_reason == "insufficient_years"

    naive_average_of_2_years = sum(
        roic_for_period(**{k: p[k] for k in ("ebit", "equity", "net_debt", "total_assets")}).value
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
        equity=healthy["equity"],
        net_debt=healthy["net_debt"],
        total_assets=healthy["total_assets"],
    )
    assert healthy_result.invalid_reason is None
    assert healthy_result.value == pytest.approx(0.1339, abs=0.001)

    bad_year = periods_by_year[2024]
    assert bad_year["equity"] + bad_year["net_debt"] <= 0
    bad_result = roic_for_period(
        ebit=bad_year["ebit"],
        equity=bad_year["equity"],
        net_debt=bad_year["net_debt"],
        total_assets=bad_year["total_assets"],
    )
    assert bad_result.value is None
    assert bad_result.invalid_reason == "ic_non_positive"


def test_roic_ic_below_asset_floor_guard_flags_whole_company():
    # OMNI28: 2022/2023 healthy (IC 2800/2880, ~33% of total_assets); 2024
    # Invested Capital = 880, positive (does not trip ic_non_positive) but
    # only 9.78% of total_assets (9000), below the 10% floor.
    company = load_company("OMNI28")
    result = roic_3yr_average(company["periods"])
    assert result.value is None
    assert result.invalid_reason == "ic_below_asset_floor"

    healthy_years = [p for p in company["periods"] if p["fiscal_year"] != 2024]
    naive_average_of_healthy_years = sum(
        roic_for_period(**{k: p[k] for k in ("ebit", "equity", "net_debt", "total_assets")}).value
        for p in healthy_years
    ) / len(healthy_years)
    assert naive_average_of_healthy_years == pytest.approx(0.1347, abs=0.001)
    assert result.value != naive_average_of_healthy_years


def test_roic_for_period_guard_ic_below_asset_floor():
    company = load_company("OMNI28")
    periods_by_year = {p["fiscal_year"]: p for p in company["periods"]}

    healthy = periods_by_year[2022]
    healthy_result = roic_for_period(
        ebit=healthy["ebit"],
        equity=healthy["equity"],
        net_debt=healthy["net_debt"],
        total_assets=healthy["total_assets"],
    )
    assert healthy_result.invalid_reason is None
    assert healthy_result.value == pytest.approx(0.1339, abs=0.001)

    bad_year = periods_by_year[2024]
    invested_capital = bad_year["equity"] + bad_year["net_debt"]
    assert invested_capital > 0
    assert invested_capital < IC_ASSET_FLOOR_RATIO * bad_year["total_assets"]
    bad_result = roic_for_period(
        ebit=bad_year["ebit"],
        equity=bad_year["equity"],
        net_debt=bad_year["net_debt"],
        total_assets=bad_year["total_assets"],
    )
    assert bad_result.value is None
    assert bad_result.invalid_reason == "ic_below_asset_floor"


def test_roic_implausibly_high_guard_flags_whole_company():
    # APEX29: 2022/2023 healthy (IC 2800/2880, ~33% of total_assets); 2024
    # Invested Capital = 1000, a healthy 33.3% of total_assets (3000) - does
    # not trip ic_below_asset_floor - but the resulting ROIC (67.5%) exceeds
    # the 60% plausibility cap.
    company = load_company("APEX29")
    result = roic_3yr_average(company["periods"])
    assert result.value is None
    assert result.invalid_reason == "roic_implausibly_high"

    healthy_years = [p for p in company["periods"] if p["fiscal_year"] != 2024]
    naive_average_of_healthy_years = sum(
        roic_for_period(**{k: p[k] for k in ("ebit", "equity", "net_debt", "total_assets")}).value
        for p in healthy_years
    ) / len(healthy_years)
    assert naive_average_of_healthy_years == pytest.approx(0.1347, abs=0.001)
    assert result.value != naive_average_of_healthy_years


def test_roic_for_period_guard_roic_implausibly_high():
    company = load_company("APEX29")
    periods_by_year = {p["fiscal_year"]: p for p in company["periods"]}

    healthy = periods_by_year[2022]
    healthy_result = roic_for_period(
        ebit=healthy["ebit"],
        equity=healthy["equity"],
        net_debt=healthy["net_debt"],
        total_assets=healthy["total_assets"],
    )
    assert healthy_result.invalid_reason is None
    assert healthy_result.value == pytest.approx(0.1339, abs=0.001)

    bad_year = periods_by_year[2024]
    invested_capital = bad_year["equity"] + bad_year["net_debt"]
    assert invested_capital >= IC_ASSET_FLOOR_RATIO * bad_year["total_assets"]  # asset-floor guard not tripped
    bad_result = roic_for_period(
        ebit=bad_year["ebit"],
        equity=bad_year["equity"],
        net_debt=bad_year["net_debt"],
        total_assets=bad_year["total_assets"],
    )
    assert bad_result.value is None
    assert bad_result.invalid_reason == "roic_implausibly_high"


def test_roic_different_invalid_reasons_are_distinguishable():
    ic_guard_result = roic_3yr_average(load_company("NAKA17")["periods"])
    insufficient_years_result = roic_3yr_average(load_company("ORBT18")["periods"])
    net_debt_unreliable_result = roic_3yr_average(load_company("MURK26")["periods"])
    ic_below_asset_floor_result = roic_3yr_average(load_company("OMNI28")["periods"])
    roic_implausibly_high_result = roic_3yr_average(load_company("APEX29")["periods"])

    reasons = {
        ic_guard_result.invalid_reason,
        insufficient_years_result.invalid_reason,
        net_debt_unreliable_result.invalid_reason,
        ic_below_asset_floor_result.invalid_reason,
        roic_implausibly_high_result.invalid_reason,
    }
    assert reasons == {
        "ic_non_positive",
        "insufficient_years",
        "net_debt_unreliable",
        "ic_below_asset_floor",
        "roic_implausibly_high",
    }


def test_roic_net_debt_unreliable_guard_flags_whole_company():
    # MURK26: net_debt=None in 2024, which is both the last of the 3 ROIC
    # years and (separately) the Net-Debt/EBITDA stichtag year - Net
    # Debt/EBITDA is flagged too for this fixture (see test_net_debt_ebitda.py).
    company = load_company("MURK26")
    result = roic_3yr_average(company["periods"])
    assert result.value is None
    assert result.invalid_reason == "net_debt_unreliable"

    healthy_years = [p for p in company["periods"] if p["fiscal_year"] != 2024]
    naive_average_of_healthy_years = sum(
        roic_for_period(**{k: p[k] for k in ("ebit", "equity", "net_debt", "total_assets")}).value
        for p in healthy_years
    ) / len(healthy_years)
    assert naive_average_of_healthy_years == pytest.approx(0.1347, abs=0.001)
    assert result.value != naive_average_of_healthy_years


def test_roic_net_debt_unreliable_flags_whole_company_even_in_an_older_year():
    # FOGG27: net_debt=None in 2022 (the OLDEST of the 3 years, not the
    # Net-Debt/EBITDA stichtag). ROIC's 3-year average is flagged by an
    # unreliable net_debt in ANY of its 3 years, regardless of position -
    # same "one bad year disqualifies the whole average" rule as
    # ic_non_positive (NAKA17). Contrast with Net Debt/EBITDA on this same
    # fixture in test_net_debt_ebitda.py, which stays valid since its
    # stichtag logic only looks at 2024.
    company = load_company("FOGG27")
    result = roic_3yr_average(company["periods"])
    assert result.value is None
    assert result.invalid_reason == "net_debt_unreliable"

    healthy_years = [p for p in company["periods"] if p["fiscal_year"] != 2022]
    naive_average_of_healthy_years = sum(
        roic_for_period(**{k: p[k] for k in ("ebit", "equity", "net_debt", "total_assets")}).value
        for p in healthy_years
    ) / len(healthy_years)
    assert naive_average_of_healthy_years == pytest.approx(0.1352, abs=0.001)
    assert result.value != naive_average_of_healthy_years


def test_roic_for_period_guard_on_net_debt_none():
    company = load_company("MURK26")
    periods_by_year = {p["fiscal_year"]: p for p in company["periods"]}

    healthy = periods_by_year[2022]
    healthy_result = roic_for_period(
        ebit=healthy["ebit"],
        equity=healthy["equity"],
        net_debt=healthy["net_debt"],
        total_assets=healthy["total_assets"],
    )
    assert healthy_result.invalid_reason is None
    assert healthy_result.value == pytest.approx(0.1339, abs=0.001)

    bad_year = periods_by_year[2024]
    assert bad_year["net_debt"] is None
    bad_result = roic_for_period(
        ebit=bad_year["ebit"],
        equity=bad_year["equity"],
        net_debt=bad_year["net_debt"],
        total_assets=bad_year["total_assets"],
    )
    assert bad_result.value is None
    assert bad_result.invalid_reason == "net_debt_unreliable"


def test_roic_normal_net_debt_is_not_treated_as_unreliable():
    # Regression guard for the net_debt_unreliable check itself: an
    # ordinary numeric net_debt (the "under 10%" informational-flag case
    # per docs/decisions.md, where a data_layer adapter keeps the real
    # value instead of substituting None) must compute exactly as before -
    # the new guard must not misfire on a real number. Uses values distinct
    # from MURK26/FOGG27's healthy years so this doesn't just re-assert an
    # already-covered case.
    result = roic_for_period(ebit=1000, equity=5000, net_debt=1500, total_assets=20000)
    assert result.invalid_reason is None
    assert result.value == pytest.approx(0.1154, abs=0.001)


def test_roic_net_debt_zero_is_not_treated_as_unreliable():
    # net_debt=0 (a genuine net-cash position, e.g. GLBX02-style) must be
    # distinguished from the net_debt_unreliable sentinel - the guard uses
    # "is None", not truthiness, so a falsy-but-valid 0 must pass through.
    result = roic_for_period(ebit=500, equity=2000, net_debt=0, total_assets=8000)
    assert result.invalid_reason is None
    assert result.value == pytest.approx(0.1875, abs=0.001)


def test_roic_total_assets_none_is_treated_as_unreliable():
    # Mirrors the net_debt=None guard: total_assets is a mandatory field,
    # but a data_layer adapter marking it unreliable (analogous to the
    # established net_debt sentinel) must not crash the whole evaluation.
    result = roic_for_period(ebit=500, equity=2000, net_debt=800, total_assets=None)
    assert result.value is None
    assert result.invalid_reason == "total_assets_unreliable"


def test_roic_total_assets_non_positive_is_treated_as_unreliable():
    # A non-positive total_assets would otherwise make the asset-floor
    # comparison's right-hand side <=0, letting any already-confirmed-
    # positive invested_capital trivially pass the guard it's meant to
    # enforce (Review-Fund, roic-total-assets-guard).
    result = roic_for_period(ebit=500, equity=2000, net_debt=800, total_assets=0)
    assert result.value is None
    assert result.invalid_reason == "total_assets_unreliable"

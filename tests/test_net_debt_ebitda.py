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


def test_net_debt_ebitda_unreliable_guard_at_stichtag():
    # MURK26: net_debt=None in 2024, the stichtag year (ROIC is flagged
    # too for this fixture, since 2024 is also one of its 3 years - see
    # test_roic.py).
    company = load_company("MURK26")
    result = net_debt_ebitda_latest(company["periods"])
    assert result.value is None
    assert result.invalid_reason == "net_debt_unreliable"

    healthy = next(p for p in company["periods"] if p["fiscal_year"] == 2022)
    healthy_result = net_debt_ebitda_for_period(net_debt=healthy["net_debt"], ebitda=healthy["ebitda"])
    assert healthy_result.invalid_reason is None
    assert healthy_result.value == pytest.approx(800 / 650, abs=0.001)


def test_net_debt_ebitda_unaffected_by_unreliable_net_debt_in_an_older_year():
    # FOGG27: net_debt=None in 2022, an OLDER year that Net-Debt/EBITDA's
    # stichtag logic never looks at (only the latest year, 2024, matters -
    # same isolation TYRL16 already demonstrates for a merely bad, not
    # unreliable, older year). Contrast with ROIC on this same fixture in
    # test_roic.py, which IS flagged, since its 3-year average has no such
    # isolation.
    company = load_company("FOGG27")
    result = net_debt_ebitda_latest(company["periods"])
    assert result.invalid_reason is None
    assert result.value == pytest.approx(800 / 700, abs=0.001)


def test_net_debt_ebitda_normal_net_debt_is_not_treated_as_unreliable():
    # Regression guard for the net_debt_unreliable check itself: an
    # ordinary numeric net_debt (the "under 10%" informational-flag case
    # per docs/decisions.md, where a data_layer adapter keeps the real
    # value instead of substituting None) must compute exactly as before -
    # the new guard must not misfire on a real number. Uses values distinct
    # from MURK26/FOGG27's healthy years so this doesn't just re-assert an
    # already-covered case.
    result = net_debt_ebitda_for_period(net_debt=1500, ebitda=1200)
    assert result.invalid_reason is None
    assert result.value == pytest.approx(1500 / 1200, abs=0.001)


def test_net_debt_ebitda_zero_is_not_treated_as_unreliable():
    # net_debt=0 (a genuine net-cash position, e.g. GLBX02-style) must be
    # distinguished from the net_debt_unreliable sentinel - the guard uses
    # "is None", not truthiness, so a falsy-but-valid 0 must pass through.
    result = net_debt_ebitda_for_period(net_debt=0, ebitda=700)
    assert result.invalid_reason is None
    assert result.value == pytest.approx(0.0, abs=0.001)


def test_net_debt_ebitda_unreliable_takes_precedence_over_ebitda_guard():
    # Pins down guard ordering per docs/decisions.md ("Guard-Reihenfolge"):
    # net_debt is checked before ebitda, so when both conditions hold at
    # once, "net_debt_unreliable" wins over "ebitda_non_positive".
    result = net_debt_ebitda_for_period(net_debt=None, ebitda=-100)
    assert result.value is None
    assert result.invalid_reason == "net_debt_unreliable"


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


def _acme_periods_with_net_debt_none_in_year(fiscal_year, data_quality_flags):
    periods = [dict(p) for p in load_company("ACME01")["periods"]]
    period = next(p for p in periods if p["fiscal_year"] == fiscal_year)
    period["net_debt"] = None
    period["data_quality_flags"] = data_quality_flags
    return periods


def test_net_debt_ebitda_debt_data_completely_missing_at_stichtag_gives_its_own_reason():
    # docs/decisions.md "SimFin-Client" Entscheidung 10: net_debt=None plus
    # the adapter's debt_data_completely_missing flag on the Stichtag year
    # makes the metric directly invalid, with its own reason.
    periods = _acme_periods_with_net_debt_none_in_year(2024, ["debt_data_completely_missing"])
    result = net_debt_ebitda_latest(periods)
    assert result.value is None
    assert result.invalid_reason == "debt_data_completely_missing"


def test_net_debt_ebitda_debt_data_completely_missing_in_an_older_year_has_no_effect():
    # Same Stichtag isolation as net_debt_unreliable in an older year (FOGG27).
    periods = _acme_periods_with_net_debt_none_in_year(2022, ["debt_data_completely_missing"])
    result = net_debt_ebitda_latest(periods)
    assert result.invalid_reason is None
    assert result.value == pytest.approx(800 / 790, abs=0.001)


def test_net_debt_ebitda_net_debt_none_without_the_completely_missing_flag_stays_unreliable():
    for flags in ([], ["debt_statement_mismatch", "pl_lags_other_statements"]):
        result = net_debt_ebitda_latest(_acme_periods_with_net_debt_none_in_year(2024, flags))
        assert result.invalid_reason == "net_debt_unreliable"


def test_net_debt_ebitda_data_quality_flags_key_present_but_null_does_not_raise():
    healthy = [dict(p, data_quality_flags=None) for p in load_company("ACME01")["periods"]]
    assert net_debt_ebitda_latest(healthy).invalid_reason is None

    unreliable = _acme_periods_with_net_debt_none_in_year(2024, None)
    assert net_debt_ebitda_latest(unreliable).invalid_reason == "net_debt_unreliable"


def test_net_debt_ebitda_for_period_net_debt_invalid_reason_parameter():
    assert net_debt_ebitda_for_period(net_debt=None, ebitda=700).invalid_reason == "net_debt_unreliable"
    result = net_debt_ebitda_for_period(
        net_debt=None, ebitda=700, net_debt_invalid_reason="debt_data_completely_missing"
    )
    assert result.invalid_reason == "debt_data_completely_missing"

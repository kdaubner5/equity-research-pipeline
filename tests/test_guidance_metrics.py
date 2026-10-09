"""Rechnungen der Stufe-3-Checkliste auf Guidance-Basis (Roadmap D.2): FCF-Rendite, EV/EBITDA, Verschuldung im
jungsten Quartal. Reine Funktionen; fehlende oder unbrauchbare Eingaben ergeben nie eine Zahl, sondern den Grund."""

import math

import pytest

from config_layer.thresholds import NetDebtEbitdaThresholds
from metrics_layer import guidance as g


def test_fcf_yield_is_fcf_over_market_cap():
    assert g.fcf_yield_guidance(120.0, 1000.0).value == pytest.approx(0.12)


def test_negative_fcf_gives_a_negative_yield_not_an_error():
    assert g.fcf_yield_guidance(-50.0, 1000.0).value == pytest.approx(-0.05)


@pytest.mark.parametrize("fcf, cap", [(None, 1000.0), (120.0, None), (120.0, 0.0), (120.0, -5.0),
                                       (float("nan"), 1000.0), (120.0, float("inf")), ("x", 1000.0), (True, 1000.0)])
def test_fcf_yield_refuses_unusable_input_with_a_reason(fcf, cap):
    result = g.fcf_yield_guidance(fcf, cap)
    assert result.value is None and result.invalid_reason in (g.INSUFFICIENT_EVIDENCE, g.MARKET_CAP_UNUSABLE)


def test_market_cap_zero_or_negative_has_its_own_reason():
    assert g.fcf_yield_guidance(10.0, 0.0).invalid_reason == g.MARKET_CAP_UNUSABLE
    assert g.fcf_yield_guidance(10.0, None).invalid_reason == g.INSUFFICIENT_EVIDENCE


def test_ev_ebitda_uses_market_cap_plus_net_debt():
    # EV = 1000 + 200 = 1200; EBITDA 150 -> 8.0
    assert g.ev_ebitda_guidance(1000.0, 200.0, 150.0).value == pytest.approx(8.0)


def test_net_cash_lowers_the_enterprise_value():
    assert g.ev_ebitda_guidance(1000.0, -300.0, 100.0).value == pytest.approx(7.0)


def test_non_positive_ebitda_is_refused_not_divided():
    assert g.ev_ebitda_guidance(1000.0, 200.0, 0.0).invalid_reason == "ebitda_non_positive"
    assert g.ev_ebitda_guidance(1000.0, 200.0, -10.0).invalid_reason == "ebitda_non_positive"


@pytest.mark.parametrize("cap, debt, ebitda", [(None, 1.0, 1.0), (1000.0, None, 1.0), (1000.0, 1.0, None), (0.0, 1.0, 1.0)])
def test_ev_ebitda_missing_inputs_are_named(cap, debt, ebitda):
    result = g.ev_ebitda_guidance(cap, debt, ebitda)
    assert result.value is None and result.invalid_reason is not None


def test_leverage_in_the_latest_quarter_is_net_debt_over_trailing_ebitda():
    assert g.net_debt_ebitda_latest_quarter(300.0, 200.0).value == pytest.approx(1.5)


def test_leverage_refuses_non_positive_ebitda_and_missing_input():
    assert g.net_debt_ebitda_latest_quarter(300.0, 0.0).invalid_reason == "ebitda_non_positive"
    assert g.net_debt_ebitda_latest_quarter(None, 200.0).invalid_reason == g.INSUFFICIENT_EVIDENCE


def test_classification_uses_the_stage1_thresholds_at_their_edges():
    thresholds = NetDebtEbitdaThresholds(clean_pass_below=2.3, fail_at_or_above=2.5)
    assert g.classify_leverage(2.29, thresholds) == "clean_pass"
    assert g.classify_leverage(2.3, thresholds) == "grey_zone"
    assert g.classify_leverage(2.49, thresholds) == "grey_zone"
    assert g.classify_leverage(2.5, thresholds) == "fail"


def test_difference_to_table_value_is_reported_or_none_if_missing():
    assert g.difference(0.12, 0.10) == pytest.approx(0.02)  # Guidance minus Tabellenwert
    assert g.difference(None, 0.12) is None and g.difference(0.1, None) is None
    assert not math.isnan(g.difference(0.1, 0.1))


def test_negative_enterprise_value_is_refused_like_the_stage2_metric():
    result = g.ev_ebitda_guidance(1000.0, -1200.0, 100.0)
    assert result.value is None and result.invalid_reason == g.EV_NON_POSITIVE


def test_zero_enterprise_value_is_refused():
    assert g.ev_ebitda_guidance(1000.0, -1000.0, 100.0).invalid_reason == g.EV_NON_POSITIVE


def test_implausibly_high_multiples_and_yields_are_refused_when_a_limit_is_given():
    assert g.ev_ebitda_guidance(1000.0, 0.0, 0.0001, implausible_above=100.0).invalid_reason == g.EV_EBITDA_IMPLAUSIBLY_HIGH
    assert g.ev_ebitda_guidance(1000.0, 0.0, 10.0, implausible_above=100.0).value == pytest.approx(100.0)  # genau an der Grenze: erlaubt
    assert g.fcf_yield_guidance(600.0, 1000.0, implausible_above=0.5).invalid_reason == g.FCF_YIELD_IMPLAUSIBLY_HIGH
    assert g.fcf_yield_guidance(500.0, 1000.0, implausible_above=0.5).value == pytest.approx(0.5)


def test_huge_integers_do_not_crash():
    assert g.fcf_yield_guidance(10**400, 1000.0).invalid_reason == g.INSUFFICIENT_EVIDENCE
    assert g.ev_ebitda_guidance(10**400, 1.0, 1.0).invalid_reason == g.INSUFFICIENT_EVIDENCE


def test_zero_fcf_gives_zero_yield():
    assert g.fcf_yield_guidance(0.0, 1000.0).value == 0.0


@pytest.mark.parametrize("bad", [True, "x", None, float("nan")])
def test_ev_ebitda_and_leverage_refuse_bool_text_and_nan(bad):
    assert g.ev_ebitda_guidance(1000.0, bad, 100.0).value is None
    assert g.ev_ebitda_guidance(1000.0, 10.0, bad).value is None
    assert g.net_debt_ebitda_latest_quarter(bad, 100.0).value is None
    assert g.net_debt_ebitda_latest_quarter(100.0, bad).value is None
    assert g.difference(bad, 0.1) is None


def test_market_cap_problem_is_reported_before_the_ebitda_problem():
    assert g.ev_ebitda_guidance(-1.0, 0.0, -1.0).invalid_reason == g.MARKET_CAP_UNUSABLE


def test_classify_leverage_refuses_nan():
    thresholds = NetDebtEbitdaThresholds(clean_pass_below=2.3, fail_at_or_above=2.5)
    with pytest.raises(ValueError):
        g.classify_leverage(float("nan"), thresholds)

"""Trend-Guard ratios per docs/decisions.md ("Trend-Guard (Masterplan Stufe 1.1, Hebel 3 aus Issue
#52)", Abschnitte 2 und 8): the window of fiscal years, revenue and EBITDA-margin ratios (last year
divided by the mean of the window) and the single-year ROIC of the last year.

Every function returns a MetricResult (exactly one of value / invalid_reason, never a bare None). The
ratios are exact fractions.Fraction values so that a ratio exactly on a threshold is decided exactly,
not by float rounding (like the Stage-2 percentiles); the gate compares them with the thresholds and
converts to float for storage. Pure, no network, no database, no defaults: windows and thresholds
come from config/trend_guard.yaml and config/thresholds.yaml.

Reasons: no_periods, trend_fundamentals_stale, trend_insufficient_years (window); non_finite_input,
revenue_non_positive, margin_mean_non_positive (ratios); the existing ROIC reasons for the last year.
"""

from fractions import Fraction

from config_layer.thresholds import RoicThresholds
from config_layer.trend_guard import TrendGuardConfig
from metrics_layer.result import MetricInputError, MetricResult, net_debt_none_reason
from metrics_layer.roic import roic_for_period
from metrics_layer.valuation import _is_finite_number


def trend_window(periods, *, as_of_year, max_fiscal_year_lag, config: TrendGuardConfig):
    """(window, None) or (None, invalid_reason).

    window = the config.years most recent periods in ascending fiscal-year order. No periods ->
    no_periods; latest fiscal year older than as_of_year - max_fiscal_year_lag -> trend_fundamentals_stale
    (same rule and same lag as Stage 2); fewer than config.min_years periods -> trend_insufficient_years
    (never averaged over fewer years, never a bad year left out). Gaps in the years are not filled and
    do not make the window invalid (the mapper may have dropped a year with a missing field). Two
    periods of the same fiscal year are a caller error (MetricInputError), like in the other metrics.
    """
    if not periods:
        return None, "no_periods"
    fiscal_years = [p["fiscal_year"] for p in periods]
    if len(fiscal_years) != len(set(fiscal_years)):
        raise MetricInputError("duplicate fiscal_year entries in periods")
    ordered = sorted(periods, key=lambda p: p["fiscal_year"])
    if ordered[-1]["fiscal_year"] < as_of_year - max_fiscal_year_lag:
        return None, "trend_fundamentals_stale"
    window = ordered[-config.years :]
    if len(window) < config.min_years:
        return None, "trend_insufficient_years"
    return window, None


def _exact(value):
    """The exact Fraction of a number by its decimal value: 0.07 is exactly 7/100, not the nearest binary
    float (same method as the thresholds in gates_layer.trend_guard). Integers are exact already."""
    return Fraction(str(value)) if isinstance(value, float) else Fraction(value)


def _last_over_mean(values):
    mean = sum(values) / len(values)
    return values[-1] / mean, mean


def revenue_ratio(window) -> MetricResult:
    """Revenue of the last year divided by the mean revenue of the window (exact Fraction).
    A revenue that is not a finite real number -> non_finite_input; any revenue <= 0 -> revenue_non_positive."""
    revenues = [p["revenue"] for p in window]
    if not all(_is_finite_number(r) for r in revenues):
        return MetricResult(value=None, invalid_reason="non_finite_input")
    if any(r <= 0 for r in revenues):
        return MetricResult(value=None, invalid_reason="revenue_non_positive")
    ratio, _ = _last_over_mean([_exact(r) for r in revenues])
    return MetricResult(value=ratio)


def ebitda_margin_ratio(window) -> MetricResult:
    """EBITDA margin of the last year (ebitda / revenue) divided by the mean of the yearly margins
    (exact Fraction). Check order: non_finite_input, revenue_non_positive, margin_mean_non_positive
    (a mean margin <= 0 has no meaningful ratio). A negative last margin with a positive mean is valid."""
    revenues = [p["revenue"] for p in window]
    ebitdas = [p["ebitda"] for p in window]
    if not all(_is_finite_number(v) for v in (*revenues, *ebitdas)):
        return MetricResult(value=None, invalid_reason="non_finite_input")
    if any(r <= 0 for r in revenues):
        return MetricResult(value=None, invalid_reason="revenue_non_positive")
    margins = [_exact(e) / _exact(r) for e, r in zip(ebitdas, revenues)]
    if sum(margins) / len(margins) <= 0:
        return MetricResult(value=None, invalid_reason="margin_mean_non_positive")
    ratio, _ = _last_over_mean(margins)
    return MetricResult(value=ratio)


def roic_last_year(window, *, thresholds: RoicThresholds) -> MetricResult:
    """The single-year ROIC of the newest period of the window, with the existing Stage-1 function and
    guards (roic_for_period). Earlier years are not read. Non-finite or wrong-typed inputs of the last
    year -> non_finite_input; an unusable net debt keeps its own reason (net_debt_unreliable or
    debt_data_completely_missing)."""
    last = window[-1]
    if not all(_is_finite_number(last[k]) for k in ("ebit", "equity", "total_assets")):
        return MetricResult(value=None, invalid_reason="non_finite_input")
    if last["net_debt"] is not None and not _is_finite_number(last["net_debt"]):
        return MetricResult(value=None, invalid_reason="non_finite_input")
    return roic_for_period(
        last["ebit"], last["equity"], last["net_debt"], last["total_assets"], thresholds=thresholds,
        net_debt_invalid_reason=net_debt_none_reason(last),
    )

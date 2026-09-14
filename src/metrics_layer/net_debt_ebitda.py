"""Net Debt/EBITDA per docs/decisions.md."""

from metrics_layer.result import MetricResult


def net_debt_ebitda_for_period(net_debt, ebitda):
    """Net Debt/EBITDA for a single fiscal year."""
    if ebitda <= 0:
        return MetricResult(value=None, invalid_reason="ebitda_non_positive")
    return MetricResult(value=net_debt / ebitda)


def net_debt_ebitda_latest(periods):
    """Net Debt/EBITDA using only the latest available fiscal year.

    Point-in-time metric, not a 3-year average like ROIC - debt is a
    balance-sheet stock, not a flow to be averaged. The grey zone
    (2.3-2.5x) and hard threshold (2.5x) are applied by the Gates-Layer
    (Issue #5), not here.
    """
    latest = max(periods, key=lambda p: p["fiscal_year"])
    return net_debt_ebitda_for_period(net_debt=latest["net_debt"], ebitda=latest["ebitda"])

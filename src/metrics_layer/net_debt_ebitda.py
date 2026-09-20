"""Net Debt/EBITDA per docs/decisions.md."""

from metrics_layer.result import MetricInputError, MetricResult


def net_debt_ebitda_for_period(net_debt, ebitda):
    """Net Debt/EBITDA for a single fiscal year.

    net_debt=None is the sentinel for "unreliable" (per docs/decisions.md,
    Net-Debt-Zuverlässigkeits-Guard) - set by a data_layer adapter when a
    debt figure contradicts another statement for this year. Guarded
    before the ebitda check and the division, both independent conditions
    on independent inputs, but data availability of net_debt itself is
    treated as more fundamental than an already-computable threshold
    guard (same precedence logic as roic_for_period's tax_rate check).
    """
    if net_debt is None:
        return MetricResult(value=None, invalid_reason="net_debt_unreliable")
    if ebitda <= 0:
        return MetricResult(value=None, invalid_reason="ebitda_non_positive")
    return MetricResult(value=net_debt / ebitda)


def net_debt_ebitda_latest(periods):
    """Net Debt/EBITDA using only the latest available fiscal year.

    Point-in-time metric, not a 3-year average like ROIC - debt is a
    balance-sheet stock, not a flow to be averaged. The grey zone
    (2.3-2.5x) and hard threshold (2.5x) are applied by the Gates-Layer
    (Issue #5), not here.

    No periods at all is a valid (if extreme) data property, flagged via
    MetricResult like insufficient_years in ROIC. Two periods sharing the
    same fiscal_year is a caller error (un-deduplicated filings, e.g. an
    original 10-K plus a 10-K/A) and raises MetricInputError instead,
    since max() would otherwise silently pick one depending on list order.
    """
    if not periods:
        return MetricResult(value=None, invalid_reason="no_periods")

    fiscal_years = [p["fiscal_year"] for p in periods]
    if len(fiscal_years) != len(set(fiscal_years)):
        raise MetricInputError("duplicate fiscal_year entries in periods")

    latest = max(periods, key=lambda p: p["fiscal_year"])
    return net_debt_ebitda_for_period(net_debt=latest["net_debt"], ebitda=latest["ebitda"])

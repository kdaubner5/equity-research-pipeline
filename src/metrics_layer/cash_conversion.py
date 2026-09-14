"""Cash Conversion (CFO / Net Income) per docs/decisions.md."""

from metrics_layer.result import MetricInputError, MetricResult


def cash_conversion_for_period(cfo, net_income):
    """Cash Conversion for a single fiscal year."""
    if net_income <= 0:
        return MetricResult(value=None, invalid_reason="ni_non_positive")
    return MetricResult(value=cfo / net_income)


def cash_conversion_3yr_average(periods):
    """3-year Cash Conversion: mean of the three independently computed yearly ratios.

    Invalid (fewer than 3 periods, e.g. recent IPO, or net_income <= 0 in any
    of the three periods) flags the whole 3-year value - never averaged over
    fewer years, extrapolated, or computed with the bad year excluded.

    Raises MetricInputError if given more than 3 periods, or if any two
    periods share the same fiscal_year - regardless of period count (this
    check runs before the insufficient_years check too, since a duplicate
    is a caller error, not a genuine data property, at any count). Both are
    caller errors (e.g. un-deduplicated multiple filings for one fiscal
    year silently standing in for a missing distinct year), not a company
    data property, so neither is a MetricResult.invalid_reason.
    """
    if len(periods) > 3:
        raise MetricInputError(f"expected at most 3 periods, got {len(periods)}")

    fiscal_years = [p["fiscal_year"] for p in periods]
    if len(fiscal_years) != len(set(fiscal_years)):
        raise MetricInputError("duplicate fiscal_year entries in periods")

    if len(periods) < 3:
        return MetricResult(value=None, invalid_reason="insufficient_years")

    yearly_ratios = []
    for period in periods:
        result = cash_conversion_for_period(
            cfo=period["cfo"],
            net_income=period["net_income"],
        )
        if result.invalid_reason is not None:
            return result
        yearly_ratios.append(result.value)
    return MetricResult(value=sum(yearly_ratios) / len(yearly_ratios))

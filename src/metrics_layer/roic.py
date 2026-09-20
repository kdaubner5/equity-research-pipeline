"""ROIC (Return on Invested Capital) per docs/decisions.md."""

from metrics_layer.result import MetricInputError, MetricResult


def roic_for_period(ebit, tax_rate, equity, net_debt):
    """ROIC for a single fiscal year.

    tax_rate=None is the sentinel for "undefined" (pretax income <= 0 for
    this year, per docs/decisions.md) - guarded before the NOPAT
    calculation even touches ebit, since there is no meaningful rate to
    apply, not even 0 or an estimate.

    net_debt=None is the sentinel for "unreliable" (per docs/decisions.md,
    Net-Debt-Zuverlässigkeits-Guard) - a data_layer adapter sets this when
    a debt figure contradicts another statement rather than merely being
    absent (unlike a plain missing value, which would default elsewhere).
    Guarded before invested_capital is computed, same reason as the
    tax_rate check: equity + None would otherwise raise a bare TypeError.
    """
    if tax_rate is None:
        return MetricResult(value=None, invalid_reason="undefined_tax_rate")
    if net_debt is None:
        return MetricResult(value=None, invalid_reason="net_debt_unreliable")
    invested_capital = equity + net_debt
    if invested_capital <= 0:
        return MetricResult(value=None, invalid_reason="ic_non_positive")
    nopat = ebit * (1 - tax_rate)
    return MetricResult(value=nopat / invested_capital)


def roic_3yr_average(periods):
    """3-year ROIC: mean of the three independently computed yearly ROICs.

    Invalid (fewer than 3 periods, e.g. recent IPO; invested capital <= 0;
    an undefined tax_rate, i.e. pretax income <= 0; or an unreliable
    net_debt, in any of the three periods) flags the whole 3-year value -
    never averaged over fewer years, extrapolated, or computed with the
    bad year excluded.

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

    yearly_roics = []
    for period in periods:
        result = roic_for_period(
            ebit=period["ebit"],
            tax_rate=period["tax_rate"],
            equity=period["equity"],
            net_debt=period["net_debt"],
        )
        if result.invalid_reason is not None:
            return result
        yearly_roics.append(result.value)
    return MetricResult(value=sum(yearly_roics) / len(yearly_roics))

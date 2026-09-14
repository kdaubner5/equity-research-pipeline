"""ROIC (Return on Invested Capital) per docs/decisions.md."""


def roic_for_period(ebit, tax_rate, equity, net_debt):
    """ROIC for a single fiscal year, or None if invested capital <= 0."""
    invested_capital = equity + net_debt
    if invested_capital <= 0:
        return None
    nopat = ebit * (1 - tax_rate)
    return nopat / invested_capital


def roic_3yr_average(periods):
    """3-year ROIC: mean of the three independently computed yearly ROICs.

    Returns None if invested capital <= 0 in any of the given periods -
    the whole 3-year value is flagged invalid, not just that year excluded
    from the average.
    """
    yearly_roics = []
    for period in periods:
        value = roic_for_period(
            ebit=period["ebit"],
            tax_rate=period["tax_rate"],
            equity=period["equity"],
            net_debt=period["net_debt"],
        )
        if value is None:
            return None
        yearly_roics.append(value)
    return sum(yearly_roics) / len(yearly_roics)

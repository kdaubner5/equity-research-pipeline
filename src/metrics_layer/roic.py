"""ROIC (Return on Invested Capital) per docs/decisions.md."""

from metrics_layer.result import MetricInputError, MetricResult

# Fixed, normalized tax rate for NOPAT (masterplan v1.1, docs/decisions.md
# "Masterplan v1.1 uebernommen") - replaces the effective per-company rate,
# which would overstate NOPAT for small caps with loss carryforwards
# (effective rate often near 0%). Module constant, not YAML, per the same
# Phase-1.0 exception as the Gates-Layer thresholds (gates_layer/stage1_gates.py).
NOPAT_TAX_RATE = 0.25

# Invested-Capital/Bilanzsummen-Guard (masterplan v1.1, docs/decisions.md
# "ROIC - Invested-Capital/Bilanzsummen-Guard") - same Phase-1.0 module-
# constant exception as NOPAT_TAX_RATE above.
IC_ASSET_FLOOR_RATIO = 0.10
ROIC_IMPLAUSIBLE_ABOVE = 0.60


def roic_for_period(ebit, equity, net_debt, total_assets):
    """ROIC for a single fiscal year.

    net_debt=None is the sentinel for "unreliable" (per docs/decisions.md,
    Net-Debt-Zuverlässigkeits-Guard) - a data_layer adapter sets this when
    a debt figure contradicts another statement rather than merely being
    absent (unlike a plain missing value, which would default elsewhere).
    Guarded before invested_capital is computed - equity + None would
    otherwise raise a bare TypeError.

    ic_below_asset_floor and roic_implausibly_high are checked via
    docs/decisions.md's Invested-Capital/Bilanzsummen-Guard - the former
    before the division (multiplication against total_assets, not a second
    division), the latter necessarily after it since it guards the computed
    ratio itself, not an input.

    total_assets=None or <=0 is guarded before use for the same reason as
    net_debt=None above (avoids a bare TypeError/silently-true comparison)
    and gets its own invalid_reason, "total_assets_unreliable" - a
    non-positive total_assets would otherwise make the asset-floor
    comparison's right-hand side <=0, letting any already-confirmed-
    positive invested_capital trivially pass the guard it's meant to
    enforce (Review-Fund, roic-total-assets-guard).
    """
    if net_debt is None:
        return MetricResult(value=None, invalid_reason="net_debt_unreliable")
    invested_capital = equity + net_debt
    if invested_capital <= 0:
        return MetricResult(value=None, invalid_reason="ic_non_positive")
    if total_assets is None or total_assets <= 0:
        return MetricResult(value=None, invalid_reason="total_assets_unreliable")
    if invested_capital < IC_ASSET_FLOOR_RATIO * total_assets:
        return MetricResult(value=None, invalid_reason="ic_below_asset_floor")
    nopat = ebit * (1 - NOPAT_TAX_RATE)
    roic = nopat / invested_capital
    if roic > ROIC_IMPLAUSIBLE_ABOVE:
        return MetricResult(value=None, invalid_reason="roic_implausibly_high")
    return MetricResult(value=roic)


def roic_3yr_average(periods):
    """3-year ROIC: mean of the three independently computed yearly ROICs.

    Invalid (fewer than 3 periods, e.g. recent IPO; invested capital <= 0;
    an unreliable net_debt or total_assets; invested capital below the
    10% Bilanzsummen floor; or an implausibly high (>60%) single-year
    ROIC, in any of the three periods) flags the whole 3-year value -
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
            equity=period["equity"],
            net_debt=period["net_debt"],
            total_assets=period["total_assets"],
        )
        if result.invalid_reason is not None:
            return result
        yearly_roics.append(result.value)
    return MetricResult(value=sum(yearly_roics) / len(yearly_roics))

"""Stage-2 valuation metrics per docs/decisions.md ("Stufe 2: Peer-Gruppen und relative
Bewertung", Abschnitte 1 und 3): EV/EBITDA, FCF-yield and the informational normalized
EV/EBITDA.

Every function returns a MetricResult (exactly one of value / invalid_reason, never a bare
None - the binding Stage-1 convention). The metrics take the periods the Stage-1 mapper
delivers (dicts with fiscal_year, ebitda, net_debt, cfo, data_quality_flags), the
company's market cap and the as_of year; no network, no database. Thresholds come from
config/stage2.yaml (config_layer.stage2), nothing is hard-coded here.

Check order per metric: the first applicable invalid_reason wins (documented in the
decision text; the tests pin it).
"""

import math

from config_layer.stage2 import Stage2Config
from metrics_layer.result import MetricInputError, MetricResult, net_debt_none_reason


def _sorted_periods(periods):
    """Periods ascending by fiscal year. Two periods of the same fiscal year are a caller
    error (un-deduplicated filings), like in the Stage-1 metrics - max() would otherwise
    silently pick one depending on list order."""
    fiscal_years = [p["fiscal_year"] for p in periods]
    if len(fiscal_years) != len(set(fiscal_years)):
        raise MetricInputError("duplicate fiscal_year entries in periods")
    return sorted(periods, key=lambda p: p["fiscal_year"])


def _is_stale(latest_period, as_of_year, config: Stage2Config):
    return latest_period["fiscal_year"] < as_of_year - config.fundamentals.max_fiscal_year_lag


def _non_finite(*values):
    """True if any given value is NaN, +-inf or an integer too large for a float (None is not
    checked: a missing value has its own reason). A NaN would otherwise compare False
    everywhere and slip through as a valid number; a huge JSON integer would crash a later
    float conversion."""
    for value in values:
        if value is None:
            continue
        try:
            if not math.isfinite(value):
                return True
        except (OverflowError, TypeError):  # an oversized integer or a value of the wrong type
            return True
    return False


def _is_real_number(value):
    """A real number (not bool, not text); finiteness is checked separately by _non_finite."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_number(value):
    """A real, finite number that fits a float (None, bool, text, NaN, inf, huge ints: False)."""
    return _is_real_number(value) and not _non_finite(value)


def _market_cap_unusable(market_cap_usd):
    return market_cap_usd is None or market_cap_usd <= 0


def _ev_and_reason(latest_period, market_cap_usd):
    """(EV, None) for the latest period, or (None, invalid_reason) if net debt is unusable.
    The caller has already checked the market cap."""
    net_debt = latest_period["net_debt"]
    if net_debt is None:
        return None, net_debt_none_reason(latest_period)
    return market_cap_usd + net_debt, None


def ev_ebitda_latest(periods, market_cap_usd, *, as_of_year, config: Stage2Config) -> MetricResult:
    """EV/EBITDA from the latest fiscal year: (market cap + net debt) / EBITDA.

    Invalid reasons in this order: no_periods, fundamentals_stale, market_cap_missing
    (None or <= 0), non_finite_input (NaN, inf or an integer too large for a float in market cap,
    EBITDA or net debt), net_debt_unreliable / debt_data_completely_missing (net debt is None),
    ebitda_non_positive, ev_non_positive (net cash exceeds the market cap), and
    ev_ebitda_implausibly_high (multiple above ev_ebitda.implausible_above; exactly on
    the cap is valid).
    """
    if not periods:
        return MetricResult(value=None, invalid_reason="no_periods")
    latest = _sorted_periods(periods)[-1]
    if _is_stale(latest, as_of_year, config):
        return MetricResult(value=None, invalid_reason="fundamentals_stale")
    if _market_cap_unusable(market_cap_usd):
        return MetricResult(value=None, invalid_reason="market_cap_missing")
    if _non_finite(market_cap_usd, latest["ebitda"], latest["net_debt"]):
        return MetricResult(value=None, invalid_reason="non_finite_input")
    ev, reason = _ev_and_reason(latest, market_cap_usd)
    if reason is not None:
        return MetricResult(value=None, invalid_reason=reason)
    if latest["ebitda"] <= 0:
        return MetricResult(value=None, invalid_reason="ebitda_non_positive")
    if ev <= 0:
        return MetricResult(value=None, invalid_reason="ev_non_positive")
    multiple = ev / latest["ebitda"]
    if multiple > config.ev_ebitda.implausible_above:
        return MetricResult(value=None, invalid_reason="ev_ebitda_implausibly_high")
    return MetricResult(value=multiple)


def ev_ebitda_normalized(periods, market_cap_usd, *, as_of_year, config: Stage2Config) -> MetricResult:
    """Informational EV/EBITDA on the mean EBITDA of the latest ev_ebitda.normalized_ebitda_years
    years, with the latest year's net debt (a stock, not averaged). Never a gate.

    Invalid reasons in this order: no_periods, insufficient_years, fundamentals_stale,
    market_cap_missing, non_finite_input, net_debt_unreliable / debt_data_completely_missing,
    ebitda_non_positive (the mean), ev_non_positive. No plausibility cap of its own.
    """
    if not periods:
        return MetricResult(value=None, invalid_reason="no_periods")
    ordered = _sorted_periods(periods)
    years = config.ev_ebitda.normalized_ebitda_years
    if len(ordered) < years:
        return MetricResult(value=None, invalid_reason="insufficient_years")
    latest = ordered[-1]
    if _is_stale(latest, as_of_year, config):
        return MetricResult(value=None, invalid_reason="fundamentals_stale")
    if _market_cap_unusable(market_cap_usd):
        return MetricResult(value=None, invalid_reason="market_cap_missing")
    window = ordered[-years:]
    if _non_finite(market_cap_usd, latest["net_debt"], *(p["ebitda"] for p in window)):
        return MetricResult(value=None, invalid_reason="non_finite_input")
    ev, reason = _ev_and_reason(latest, market_cap_usd)
    if reason is not None:
        return MetricResult(value=None, invalid_reason=reason)
    mean_ebitda = sum(p["ebitda"] for p in window) / years
    if not math.isfinite(mean_ebitda):
        return MetricResult(value=None, invalid_reason="non_finite_input")
    if mean_ebitda <= 0:
        return MetricResult(value=None, invalid_reason="ebitda_non_positive")
    if ev <= 0:
        return MetricResult(value=None, invalid_reason="ev_non_positive")
    value = ev / mean_ebitda
    if not math.isfinite(value):  # finite inputs can still overflow (e.g. a huge mean EBITDA divides to 0.0 / inf)
        return MetricResult(value=None, invalid_reason="non_finite_input")
    return MetricResult(value=value)


def _capex_outflow(change_in_fixed_assets):
    """Capex as a positive outflow from SimFin's net line (negative = money paid out).
    A net inflow (positive value, asset sales) counts as 0 - not as extra cash."""
    return max(0.0, -change_in_fixed_assets)


def _fcf_window(periods, config: Stage2Config):
    return _sorted_periods(periods)[-config.fcf_yield.years :]


def _fcf_yield(periods, capex_by_year, other_investing_by_year, market_cap_usd, *, as_of_year, config: Stage2Config):
    """Shared body of fcf_yield_average and fcf_yield_incl_other_investing. other_investing_by_year
    None = the main value; a mapping = the counter-calculation (see there)."""
    if not periods:
        return MetricResult(value=None, invalid_reason="no_periods")
    ordered = _sorted_periods(periods)
    years = config.fcf_yield.years
    if len(ordered) < years:
        return MetricResult(value=None, invalid_reason="insufficient_years")
    if _is_stale(ordered[-1], as_of_year, config):
        return MetricResult(value=None, invalid_reason="fundamentals_stale")
    if _market_cap_unusable(market_cap_usd):
        return MetricResult(value=None, invalid_reason="market_cap_missing")
    window = ordered[-years:]
    changes = [capex_by_year.get(p["fiscal_year"]) for p in window]
    others = [] if other_investing_by_year is None else [other_investing_by_year.get(p["fiscal_year"]) for p in window]
    if _non_finite(market_cap_usd, *changes, *others, *(p["cfo"] for p in window)) or any(
        v is not None and not _is_real_number(v) for v in others
    ):
        return MetricResult(value=None, invalid_reason="non_finite_input")
    if any(change is None for change in changes):
        return MetricResult(value=None, invalid_reason="capex_missing")
    if other_investing_by_year is None:
        outflows = [_capex_outflow(change) for change in changes]
    else:
        # Only an OUTFLOW of Other Investing is added to the capex line's own outflow. An inflow is not
        # netted against the capex (that would make the counter-calculation look better than the main
        # value, which a conservative check must never do), and a missing year counts 0: no more
        # outflow is assumed than is documented.
        outflows = [_capex_outflow(change) + max(0.0, -(other or 0)) for change, other in zip(changes, others)]
    fcfs = [p["cfo"] - outflow for p, outflow in zip(window, outflows)]
    # mean first, then the division: exact for round test figures (60/1000 == 0.06).
    mean_fcf = sum(fcfs) / years
    value = mean_fcf / market_cap_usd
    if not math.isfinite(mean_fcf) or not math.isfinite(value):  # finite inputs can still overflow
        return MetricResult(value=None, invalid_reason="non_finite_input")
    if value > config.fcf_yield.implausible_above:
        return MetricResult(value=None, invalid_reason="fcf_yield_implausibly_high")
    return MetricResult(value=value)


def fcf_yield_average(periods, capex_by_year, market_cap_usd, *, as_of_year, config: Stage2Config) -> MetricResult:
    """FCF-yield: mean of (CFO - capex outflow) over the latest fcf_yield.years years,
    divided by the current market cap. capex_by_year maps fiscal_year -> SimFin's
    "Change in Fixed Assets & Intangibles" (data_layer.simfin_valuation).

    A missing capex (None, or the year absent from the map) is never read as 0:
    capex_missing. A negative FCF is a valid, negative yield.

    Invalid reasons in this order: no_periods, insufficient_years (fewer periods than
    fcf_yield.years), fundamentals_stale, market_cap_missing, non_finite_input (NaN/inf in market cap,
    CFO or capex of the window), capex_missing, fcf_yield_implausibly_high (yield above
    fcf_yield.implausible_above; exactly on the cap is valid).
    """
    return _fcf_yield(periods, capex_by_year, None, market_cap_usd, as_of_year=as_of_year, config=config)


def fcf_yield_incl_other_investing(
    periods, capex_by_year, other_investing_by_year, market_cap_usd, *, as_of_year, config: Stage2Config
) -> MetricResult:
    """Counter-calculation of the FCF-yield (docs/decisions.md "Stufe 2 - Datenplausibilitaet",
    Abschnitt 2): the same window and rules as fcf_yield_average, but the outflow of a year is
    max(0, -(capex line + Other Investing)): Other Investing is counted with its sign, a net inflow
    still counts as 0 outflow (never as extra cash), and a year without a value counts 0.
    The result is never above the main value. Same invalid reasons as fcf_yield_average; a NaN, inf,
    oversized or wrong-typed Other Investing value is non_finite_input.
    """
    return _fcf_yield(
        periods, capex_by_year, dict(other_investing_by_year), market_cap_usd, as_of_year=as_of_year, config=config
    )


def other_investing_triggered(periods, capex_by_year, other_investing_by_year, *, config: Stage2Config):
    """True if in at least one year of the FCF window |Other Investing| is more than
    other_investing_check.min_ratio_to_capex times |capex line| (strict; with a capex line of 0 any
    Other Investing != 0 triggers), False if every readable year is below, None if no year could be
    read (Other Investing or capex missing, NaN, inf, oversized or of the wrong type in every year).
    """
    ratio = config.other_investing_check.min_ratio_to_capex
    readable = False
    for p in _fcf_window(periods, config):
        change = capex_by_year.get(p["fiscal_year"])
        other = other_investing_by_year.get(p["fiscal_year"])
        if not _is_finite_number(change) or not _is_finite_number(other):
            continue
        readable = True
        if abs(other) > ratio * abs(change):
            return True
    return False if readable else None


def net_inflow_fiscal_years(periods, capex_by_year, *, config: Stage2Config):
    """Fiscal years (ascending) within the FCF window whose capex line is a net inflow
    (value > 0) and was therefore counted as capex 0. Years without a value are not
    inflows (they make the yield invalid instead)."""
    window = _fcf_window(periods, config)
    return tuple(
        p["fiscal_year"]
        for p in window
        if capex_by_year.get(p["fiscal_year"]) is not None and capex_by_year[p["fiscal_year"]] > 0
    )

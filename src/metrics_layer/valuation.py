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
    """True if any given value is NaN or +-inf (None is not checked: a missing value has its own reason).
    A NaN would otherwise compare False everywhere and slip through as a valid number."""
    return any(v is not None and not math.isfinite(v) for v in values)


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
    (None or <= 0), net_debt_unreliable / debt_data_completely_missing (net debt is None),
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
    if mean_ebitda <= 0:
        return MetricResult(value=None, invalid_reason="ebitda_non_positive")
    if ev <= 0:
        return MetricResult(value=None, invalid_reason="ev_non_positive")
    return MetricResult(value=ev / mean_ebitda)


def _capex_outflow(change_in_fixed_assets):
    """Capex as a positive outflow from SimFin's net line (negative = money paid out).
    A net inflow (positive value, asset sales) counts as 0 - not as extra cash."""
    return max(0.0, -change_in_fixed_assets)


def _fcf_window(periods, config: Stage2Config):
    return _sorted_periods(periods)[-config.fcf_yield.years :]


def fcf_yield_average(periods, capex_by_year, market_cap_usd, *, as_of_year, config: Stage2Config) -> MetricResult:
    """FCF-yield: mean of (CFO - capex outflow) over the latest fcf_yield.years years,
    divided by the current market cap. capex_by_year maps fiscal_year -> SimFin's
    "Change in Fixed Assets & Intangibles" (data_layer.simfin_valuation).

    A missing capex (None, or the year absent from the map) is never read as 0:
    capex_missing. A negative FCF is a valid, negative yield.

    Invalid reasons in this order: no_periods, insufficient_years (fewer periods than
    fcf_yield.years), fundamentals_stale, market_cap_missing, non_finite_input (NaN/inf in market cap,
    CFO or capex of the window), capex_missing, fcf_yield_implausibly_high (yield above fcf_yield.implausible_above; exactly on the
    cap is valid).
    """
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
    if _non_finite(market_cap_usd, *changes, *(p["cfo"] for p in window)):
        return MetricResult(value=None, invalid_reason="non_finite_input")
    if any(change is None for change in changes):
        return MetricResult(value=None, invalid_reason="capex_missing")
    fcfs = [p["cfo"] - _capex_outflow(change) for p, change in zip(window, changes)]
    # mean first, then the division: exact for round test figures (60/1000 == 0.06).
    value = (sum(fcfs) / years) / market_cap_usd
    if value > config.fcf_yield.implausible_above:
        return MetricResult(value=None, invalid_reason="fcf_yield_implausibly_high")
    return MetricResult(value=value)


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

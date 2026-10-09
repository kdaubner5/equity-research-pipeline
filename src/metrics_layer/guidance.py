"""Rechnungen der Stufe-3-Checkliste auf Guidance-Basis (docs/stufe3/checkliste.md, Punkte 3 und 4; Roadmap D.2).

Reine Funktionen: Die Eingaben sind Zahlen, die ein Mensch (oder der Agent) aus einer Primaerquelle uebernommen hat
(Earnings-Call-Transkript, 10-Q); das Sprachmodell rechnet nicht im Kopf. Alle Betraege in derselben Einheit und
Waehrung. Fehlende oder unbrauchbare Eingaben ergeben nie eine Zahl, sondern einen Grund (MetricResult), wie bei den
Stufe-1-Kennzahlen. Enterprise Value wie im Bestand: Marktkapitalisierung + Nettoverschuldung.
"""

import math
from typing import Optional

from config_layer.thresholds import NetDebtEbitdaThresholds
from metrics_layer.result import MetricResult

INSUFFICIENT_EVIDENCE = "insufficient_evidence"
MARKET_CAP_UNUSABLE = "market_cap_unusable"
EBITDA_NON_POSITIVE = "ebitda_non_positive"
EV_NON_POSITIVE = "ev_non_positive"
EV_EBITDA_IMPLAUSIBLY_HIGH = "ev_ebitda_implausibly_high"
FCF_YIELD_IMPLAUSIBLY_HIGH = "fcf_yield_implausibly_high"

CLEAN_PASS = "clean_pass"
GREY_ZONE = "grey_zone"
FAIL = "fail"


def _number(value) -> Optional[float]:
    """Endliche Zahl oder None (kein Bool, kein Text, kein NaN, kein Unendlich)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # zum Beispiel 10**400
        return None
    return number if math.isfinite(number) else None


def fcf_yield_guidance(fcf_guidance, market_cap_usd, *, implausible_above=None) -> MetricResult:
    """FCF-Rendite = freier Cashflow (Guidance) / Marktkapitalisierung. Negativer FCF ergibt eine negative Rendite.

    implausible_above (aus config/stage2.yaml, fcf_yield) lehnt eine unplausibel hohe Rendite ab (Einheitenfehler?)."""
    fcf, cap = _number(fcf_guidance), _number(market_cap_usd)
    if fcf is None or cap is None:
        return MetricResult(value=None, invalid_reason=INSUFFICIENT_EVIDENCE)
    if cap <= 0:
        return MetricResult(value=None, invalid_reason=MARKET_CAP_UNUSABLE)
    value = fcf / cap
    if implausible_above is not None and value > implausible_above:
        return MetricResult(value=None, invalid_reason=FCF_YIELD_IMPLAUSIBLY_HIGH)
    return MetricResult(value=value)


def ev_ebitda_guidance(market_cap_usd, net_debt, ebitda_guidance, *, implausible_above=None) -> MetricResult:
    """EV/EBITDA = (Marktkapitalisierung + Nettoverschuldung) / EBITDA (Guidance). Nettocash (negative Schulden) senkt den EV."""
    cap, debt, ebitda = _number(market_cap_usd), _number(net_debt), _number(ebitda_guidance)
    if cap is None or debt is None or ebitda is None:
        return MetricResult(value=None, invalid_reason=INSUFFICIENT_EVIDENCE)
    if cap <= 0:
        return MetricResult(value=None, invalid_reason=MARKET_CAP_UNUSABLE)
    if ebitda <= 0:
        return MetricResult(value=None, invalid_reason=EBITDA_NON_POSITIVE)
    enterprise_value = cap + debt
    if enterprise_value <= 0:
        return MetricResult(value=None, invalid_reason=EV_NON_POSITIVE)
    value = enterprise_value / ebitda
    if implausible_above is not None and value > implausible_above:
        return MetricResult(value=None, invalid_reason=EV_EBITDA_IMPLAUSIBLY_HIGH)
    return MetricResult(value=value)


def net_debt_ebitda_latest_quarter(net_debt_quarter, ebitda_trailing_twelve_months) -> MetricResult:
    """Net Debt/EBITDA mit der Nettoverschuldung des juengsten Quartals und dem EBITDA der letzten zwoelf Monate."""
    debt, ebitda = _number(net_debt_quarter), _number(ebitda_trailing_twelve_months)
    if debt is None or ebitda is None:
        return MetricResult(value=None, invalid_reason=INSUFFICIENT_EVIDENCE)
    if ebitda <= 0:
        return MetricResult(value=None, invalid_reason=EBITDA_NON_POSITIVE)
    return MetricResult(value=debt / ebitda)


def classify_leverage(value, thresholds: NetDebtEbitdaThresholds) -> str:
    """Einordnung gegen die Stufe-1-Schwellen aus config/thresholds.yaml (unter clean_pass_below sauber, ab
    fail_at_or_above durchgefallen, dazwischen Grauzone). Kein endlicher Wert: ValueError, nie eine stille Einordnung."""
    if _number(value) is None:
        raise ValueError(f"value must be a finite number, got {value!r}")
    if value < thresholds.clean_pass_below:
        return CLEAN_PASS
    if value >= thresholds.fail_at_or_above:
        return FAIL
    return GREY_ZONE


def difference(guidance_value, table_value) -> Optional[float]:
    """Guidance-Wert minus Tabellenwert, oder None, wenn einer fehlt."""
    a, b = _number(guidance_value), _number(table_value)
    if a is None or b is None:
        return None
    return a - b

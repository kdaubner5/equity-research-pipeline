"""Rechnungen der Stufe-3-Checkliste auf Guidance-Basis (docs/stufe3/checkliste.md, Punkte 3 und 4; Roadmap D.2).

Reine Funktionen: Die Eingaben sind Zahlen, die ein Mensch (oder der Agent) aus einer Primaerquelle uebernommen hat
(Earnings-Call-Transkript, 10-Q); das Sprachmodell rechnet nicht im Kopf. Alle Betraege in derselben Einheit und
Waehrung. Fehlende oder unbrauchbare Eingaben ergeben nie eine Zahl, sondern einen Grund (MetricResult), wie bei den
Stufe-1-Kennzahlen. Enterprise Value wie im Bestand: Marktkapitalisierung + Nettoverschuldung.
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from config_layer.thresholds import NetDebtEbitdaThresholds
from metrics_layer.result import MetricResult

INSUFFICIENT_EVIDENCE = "insufficient_evidence"
MARKET_CAP_UNUSABLE = "market_cap_unusable"
EBITDA_NON_POSITIVE = "ebitda_non_positive"
EV_NON_POSITIVE = "ev_non_positive"
EV_EBITDA_IMPLAUSIBLY_HIGH = "ev_ebitda_implausibly_high"
FCF_YIELD_IMPLAUSIBLY_HIGH = "fcf_yield_implausibly_high"
FCF_NON_POSITIVE = "fcf_non_positive"
SHARES_UNUSABLE = "shares_unusable"
TARGET_UNUSABLE = "target_unusable"
LIMIT_NON_POSITIVE = "limit_non_positive"

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


# ---- Einstiegsgrenze: regelbasiert, keine Prognose ---------------------------------------------------------------
# Der Preis je Aktie, bis zu dem die Firma die eigenen Bewertungsgrenzen aus config/stage2.yaml noch erfuellt, gerechnet
# mit den heutigen Zahlen (FCF, EBITDA, Nettoschulden, Aktienzahl). Es ist kein Kursziel und keine Aussage darueber, wohin
# der Kurs geht; ein anderer Zahlenstand ergibt eine andere Grenze.


def price_limit_fcf_yield(fcf, shares, target_yield) -> MetricResult:
    """Preis je Aktie, bei dem die FCF-Rendite genau die Zielrendite erreicht: FCF / Zielrendite / Aktienzahl."""
    fcf_n, shares_n, target_n = _number(fcf), _number(shares), _number(target_yield)
    if fcf_n is None or shares_n is None or target_n is None:
        return MetricResult(value=None, invalid_reason=INSUFFICIENT_EVIDENCE)
    if fcf_n <= 0:
        return MetricResult(value=None, invalid_reason=FCF_NON_POSITIVE)
    if shares_n <= 0:
        return MetricResult(value=None, invalid_reason=SHARES_UNUSABLE)
    if target_n <= 0:
        return MetricResult(value=None, invalid_reason=TARGET_UNUSABLE)
    return MetricResult(value=fcf_n / target_n / shares_n)


def price_limit_ev_ebitda(ebitda, net_debt, shares, target_multiple) -> MetricResult:
    """Preis je Aktie, bei dem EV/EBITDA genau das Zielvielfache erreicht: (Zielvielfaches x EBITDA - Nettoschulden) / Aktienzahl."""
    ebitda_n, debt_n, shares_n, target_n = _number(ebitda), _number(net_debt), _number(shares), _number(target_multiple)
    if ebitda_n is None or debt_n is None or shares_n is None or target_n is None:
        return MetricResult(value=None, invalid_reason=INSUFFICIENT_EVIDENCE)
    if ebitda_n <= 0:
        return MetricResult(value=None, invalid_reason=EBITDA_NON_POSITIVE)
    if shares_n <= 0:
        return MetricResult(value=None, invalid_reason=SHARES_UNUSABLE)
    if target_n <= 0:
        return MetricResult(value=None, invalid_reason=TARGET_UNUSABLE)
    equity_value = target_n * ebitda_n - debt_n
    if equity_value <= 0:
        return MetricResult(value=None, invalid_reason=LIMIT_NON_POSITIVE)
    return MetricResult(value=equity_value / shares_n)


@dataclass(frozen=True)
class EntryLimit:
    price: Optional[float]
    binding: Optional[str]  # Name der Grenze, die den niedrigeren Preis ergibt
    missing: List[str] = field(default_factory=list)  # Grenzen, die nicht berechenbar waren
    limits: Tuple[Tuple[str, float], ...] = ()


def entry_limit(limits: Sequence[Tuple[str, MetricResult]]) -> EntryLimit:
    """Die niedrigere der berechenbaren Preisgrenzen (die strengere gilt); nicht berechenbare werden benannt, nie ersetzt."""
    available = [(name, result.value) for name, result in limits if result.value is not None]
    missing = [name for name, result in limits if result.value is None]
    if not available:
        return EntryLimit(price=None, binding=None, missing=missing)
    name, price = min(available, key=lambda item: item[1])
    return EntryLimit(price=price, binding=name, missing=missing, limits=tuple(available))


def distance_to_limit(limit_price, current_price) -> Optional[float]:
    """Aktueller Kurs relativ zur Grenze: positiv = Kurs liegt ueber der Grenze (zu teuer), negativ = darunter (Spielraum)."""
    limit_n, price_n = _number(limit_price), _number(current_price)
    if limit_n is None or price_n is None or limit_n <= 0 or price_n <= 0:
        return None
    return price_n / limit_n - 1

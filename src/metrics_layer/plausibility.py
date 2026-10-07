"""Share-count plausibility via the dividend (docs/decisions.md "Stufe 2 - Datenplausibilitaet:
Aktienzahl und Investitionen", Abschnitt 1).

R = (dividends paid in the last fiscal year / share count) / last dividend per share. With a fitting
share count R sits near the payment rhythm of the company (4 quarterly, 2 half-yearly, 1 yearly,
12 monthly). A share count that is too SMALL inflates R and makes the market cap - and with it the
valuation - look too cheap; a count that is too large only makes a company look dear, which costs
no precision, so there is only an upper check.

Returns a MetricResult (exactly one of value / invalid_reason). Invalid reasons: dividend_data_missing
(any input None), non_finite_input (NaN, inf, an integer too large for a float, or a value of the
wrong type), dividend_not_positive (nothing was paid, or the cash-flow value is an inflow),
shares_not_positive. Thresholds come from config/stage2.yaml, nothing is hard-coded here.
"""

import math

from config_layer.stage2 import Stage2Config
from metrics_layer.result import MetricResult

SHARE_OK = "ok"
SHARE_AMBIGUOUS = "ambiguous"
SHARE_SUSPECT = "suspect"
SHARE_UNCHECKED = "unchecked"


def _usable_number(value):
    """True for a finite real number (not bool); False for a wrong type, NaN, inf or an integer that
    does not fit a float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def share_count_ratio(dividends_paid, shares, dividend_per_share) -> MetricResult:
    """R for one company. dividends_paid is the raw cash-flow value (negative = paid out)."""
    if dividends_paid is None or shares is None or dividend_per_share is None:
        return MetricResult(value=None, invalid_reason="dividend_data_missing")
    if not all(_usable_number(v) for v in (dividends_paid, shares, dividend_per_share)):
        return MetricResult(value=None, invalid_reason="non_finite_input")
    paid = -dividends_paid
    if paid <= 0 or dividend_per_share <= 0:
        return MetricResult(value=None, invalid_reason="dividend_not_positive")
    if shares <= 0:
        return MetricResult(value=None, invalid_reason="shares_not_positive")
    ratio = (paid / shares) / dividend_per_share
    if not math.isfinite(ratio):  # finite inputs can still overflow
        return MetricResult(value=None, invalid_reason="non_finite_input")
    return MetricResult(value=ratio)


def classify_share_count_ratio(ratio, config: Stage2Config) -> str:
    """SHARE_SUSPECT above the threshold unless R lies in the monthly-payer band (both edges
    inclusive: SHARE_AMBIGUOUS); everything else SHARE_OK. Exactly on the threshold is not suspect."""
    check = config.share_count_check
    if check.monthly_band_low <= ratio <= check.monthly_band_high:
        return SHARE_AMBIGUOUS
    if ratio > check.suspect_ratio_above:
        return SHARE_SUSPECT
    return SHARE_OK

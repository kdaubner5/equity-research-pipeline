"""Stage-1 Gates: boundary classification, flags and ranking per docs/decisions.md.

Combines the three raw Stage-1 MetricResults (ROIC, Net Debt/EBITDA, Cash
Conversion) into one GateResult per company - the hardest single result
wins, any invalid_reason disqualifies into its own "not_evaluable" status,
and the rank output is a flag-count grouping, never a composite score.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from metrics_layer.result import MetricResult

# Thresholds per docs/decisions.md ("Grenzwert-Konvention" + per-metric grey
# zones). Module constants, not YAML - config-driven thresholds are Phase 1.1
# (docs/masterplan.md §7), Issue #5 is Phase 1.0.
ROIC_CLEAN_PASS_ABOVE = 0.12
ROIC_FAIL_BELOW = 0.10

NET_DEBT_EBITDA_CLEAN_PASS_BELOW = 2.3
NET_DEBT_EBITDA_FAIL_AT_OR_ABOVE = 2.5

CASH_CONVERSION_CLEAN_PASS_AT_OR_ABOVE = 1.0
CASH_CONVERSION_FAIL_BELOW = 0.8

PASSED = "passed"
PASSED_WITH_FLAGS = "passed_with_flags"
FAILED = "failed"
NOT_EVALUABLE = "not_evaluable"

_METRIC_NAMES = ("roic", "net_debt_ebitda", "cash_conversion")


@dataclass(frozen=True)
class GateResult:
    """Combined Stage-1 gate result for one company.

    flags and invalid_reasons follow the fixed metric order roic /
    net_debt_ebitda / cash_conversion. The three raw values are always
    populated where the underlying MetricResult had one, regardless of
    status - a not_evaluable company keeps whichever raw values were valid.

    flags and invalid_reasons are tuples, not lists: frozen=True only stops
    a field from being reassigned, not a mutable field's contents from being
    changed in place, and a list field would also make GateResult unhashable.
    """

    status: str
    flags: Tuple[str, ...]
    invalid_reasons: Tuple[str, ...]
    roic: Optional[float]
    net_debt_ebitda: Optional[float]
    cash_conversion: Optional[float]


def _classify_roic(value):
    if value > ROIC_CLEAN_PASS_ABOVE:
        return PASSED
    if value < ROIC_FAIL_BELOW:
        return FAILED
    return PASSED_WITH_FLAGS


def _classify_net_debt_ebitda(value):
    if value < NET_DEBT_EBITDA_CLEAN_PASS_BELOW:
        return PASSED
    if value >= NET_DEBT_EBITDA_FAIL_AT_OR_ABOVE:
        return FAILED
    return PASSED_WITH_FLAGS


def _classify_cash_conversion(value):
    if value >= CASH_CONVERSION_CLEAN_PASS_AT_OR_ABOVE:
        return PASSED
    if value < CASH_CONVERSION_FAIL_BELOW:
        return FAILED
    return PASSED_WITH_FLAGS


_CLASSIFIERS = {
    "roic": _classify_roic,
    "net_debt_ebitda": _classify_net_debt_ebitda,
    "cash_conversion": _classify_cash_conversion,
}


def evaluate_stage1_gates(
    roic: MetricResult,
    net_debt_ebitda: MetricResult,
    cash_conversion: MetricResult,
) -> GateResult:
    """Combine the three Stage-1 MetricResults into one GateResult.

    Any invalid_reason among the three inputs disqualifies the company into
    "not_evaluable", independent of and never mixed with passed/flagged/
    failed. Otherwise the hardest single per-metric classification wins:
    any "failed" makes the company overall failed; else any flag makes it
    passed_with_flags (listing every flagged metric, not just the first);
    else passed.
    """
    results = {"roic": roic, "net_debt_ebitda": net_debt_ebitda, "cash_conversion": cash_conversion}
    raw_values = {name: results[name].value for name in _METRIC_NAMES}

    invalid_reasons = tuple(
        results[name].invalid_reason for name in _METRIC_NAMES if results[name].invalid_reason is not None
    )
    if invalid_reasons:
        return GateResult(
            status=NOT_EVALUABLE,
            flags=(),
            invalid_reasons=invalid_reasons,
            **raw_values,
        )

    classifications = {name: _CLASSIFIERS[name](raw_values[name]) for name in _METRIC_NAMES}
    flags = tuple(name for name in _METRIC_NAMES if classifications[name] == PASSED_WITH_FLAGS)

    if any(classification == FAILED for classification in classifications.values()):
        status = FAILED
    elif flags:
        status = PASSED_WITH_FLAGS
    else:
        status = PASSED

    return GateResult(status=status, flags=flags, invalid_reasons=(), **raw_values)


def rank_companies(gate_results: Dict[str, GateResult]) -> List[Tuple[str, GateResult]]:
    """Rank passed/passed_with_flags companies by flag-count group, then ticker.

    Groups ascending by number of flags (0 before 1 before 2, ...), tickers
    sorted alphabetically within a group. No composite score. Failed and
    not_evaluable companies are excluded entirely - not just unranked, but
    absent from the returned list.
    """
    eligible = [
        (ticker, result)
        for ticker, result in gate_results.items()
        if result.status in (PASSED, PASSED_WITH_FLAGS)
    ]
    return sorted(eligible, key=lambda item: (len(item[1].flags), item[0]))

"""Stage-1 Gates: boundary classification, flags and ranking per docs/decisions.md.

Combines the four raw Stage-1 MetricResults (ROIC, Net Debt/EBITDA, Cash
Conversion, Net Income Margin) into one GateResult per company - the
hardest single result wins, an invalid_reason disqualifies into its own
"not_evaluable" status unless a valid ROIC / Net Debt/EBITDA / Cash
Conversion is already in its hard-fail zone (then "failed", the
invalid_reasons stay recorded - docs/decisions.md, "Phase 2.0d"), and the
rank output is a flag-count grouping, never a composite score.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from config_layer.thresholds import (
    CashConversionThresholds,
    NetDebtEbitdaThresholds,
    NetIncomeMarginThresholds,
    RoicThresholds,
    Thresholds,
)
from metrics_layer.result import MetricResult

PASSED = "passed"
PASSED_WITH_FLAGS = "passed_with_flags"
FAILED = "failed"
NOT_EVALUABLE = "not_evaluable"

_METRIC_NAMES = ("roic", "net_debt_ebitda", "cash_conversion", "net_income_margin")

# Metrics whose valid hard fail beats another metric's invalid_reason
# (docs/decisions.md, "Phase 2.0d", Entscheidung 1). net_income_margin is
# deliberately not in here (Praezisierung 2): Net-Income-Margin-Gate,
# Entscheidung 4 keeps invalid_reason ahead of the margin classification.
_HARD_FAIL_PRECEDENCE_METRICS = ("roic", "net_debt_ebitda", "cash_conversion")


@dataclass(frozen=True)
class GateResult:
    """Combined Stage-1 gate result for one company.

    flags and invalid_reasons follow the fixed metric order roic /
    net_debt_ebitda / cash_conversion / net_income_margin. The four raw
    values are always populated where the underlying MetricResult had one,
    regardless of status - a not_evaluable company keeps whichever raw
    values were valid.

    invalid_reasons is non-empty for every not_evaluable company, but also
    for a "failed" one whose valid ROIC / Net Debt/EBITDA / Cash Conversion
    is in its hard-fail zone while another metric is invalid (docs/
    decisions.md, "Phase 2.0d"): "failed" no longer implies empty
    invalid_reasons, and its flags come from the valid metrics only.

    flags and invalid_reasons are tuples, not lists: frozen=True only stops
    a field from being reassigned, not a mutable field's contents from being
    changed in place, and a list field would also make GateResult unhashable.

    data_quality_flags is the company's distinct data-quality flags from the
    data layer (see collect_data_quality_flags), carried through so the store
    layer can persist them (docs/decisions.md, "net_debt-Zuverlaessigkeit -
    Stock-Flow-Abgleich", Entscheidung 5). Informational only - they never
    influence status, flags or invalid_reasons.
    """

    status: str
    flags: Tuple[str, ...]
    invalid_reasons: Tuple[str, ...]
    roic: Optional[float]
    net_debt_ebitda: Optional[float]
    cash_conversion: Optional[float]
    net_income_margin: Optional[float]
    data_quality_flags: Tuple[str, ...] = ()


def collect_data_quality_flags(periods) -> Tuple[str, ...]:
    """A company's distinct data_quality_flags across all its periods, sorted.

    The year attribution is deliberately dropped (company-level, like
    flags/invalid_reasons). Periods without a data_quality_flags key (the
    dummy fixtures) or with a null value contribute nothing. The caller that
    builds gate results (the future runner, Issue #9) passes the result to
    evaluate_stage1_gates(); the store layer never sees periods.
    """
    collected = set()
    for period in periods:
        collected.update(period.get("data_quality_flags") or ())
    return tuple(sorted(collected))


def _classify_roic(value, thresholds: RoicThresholds):
    if value > thresholds.clean_pass_above:
        return PASSED
    if value < thresholds.fail_below:
        return FAILED
    return PASSED_WITH_FLAGS


def _classify_net_debt_ebitda(value, thresholds: NetDebtEbitdaThresholds):
    if value < thresholds.clean_pass_below:
        return PASSED
    if value >= thresholds.fail_at_or_above:
        return FAILED
    return PASSED_WITH_FLAGS


def _classify_cash_conversion(value, thresholds: CashConversionThresholds):
    if value >= thresholds.clean_pass_at_or_above:
        return PASSED
    if value < thresholds.fail_below:
        return FAILED
    return PASSED_WITH_FLAGS


def _classify_net_income_margin(value, thresholds: NetIncomeMarginThresholds):
    """Binary classification, no grey zone - see NetIncomeMarginThresholds.fail_below."""
    if value < thresholds.fail_below:
        return FAILED
    return PASSED


_CLASSIFIERS = {
    "roic": _classify_roic,
    "net_debt_ebitda": _classify_net_debt_ebitda,
    "cash_conversion": _classify_cash_conversion,
    "net_income_margin": _classify_net_income_margin,
}


def evaluate_stage1_gates(
    roic: MetricResult,
    net_debt_ebitda: MetricResult,
    cash_conversion: MetricResult,
    net_income_margin: MetricResult,
    *,
    thresholds: Thresholds,
    data_quality_flags: Tuple[str, ...] = (),
) -> GateResult:
    """Combine the four Stage-1 MetricResults into one GateResult.

    Each valid metric (invalid_reason is None) is classified on its own.
    Any invalid_reason among the four inputs disqualifies the company into
    "not_evaluable", never mixed with passed/flagged - with one exception
    (docs/decisions.md, "Phase 2.0d", Issue #35): if a *valid* ROIC, Net
    Debt/EBITDA or Cash Conversion is already in its hard-fail zone, the
    company is "failed" - a single valid hard fail is reason enough and
    another metric's missing data cannot cure it. All invalid_reasons stay
    recorded on that failed result, and its flags come from the valid
    metrics. net_income_margin is excluded from this exception: a margin
    below its threshold never overrides an invalid_reason (e.g. net_income
    <= 0 and net_income/revenue < 2% in the same year: cash_conversion's
    ni_non_positive guard forces not_evaluable, docs/decisions.md
    "Net-Income-Margin-Gate" Entscheidung 4). An invalid metric is never a
    hard fail itself, whatever else it carries.
    With no invalid_reason at all the hardest single per-metric
    classification wins: any "failed" (the margin included) makes the
    company overall failed; else any flag makes it passed_with_flags
    (listing every flagged metric, not just the first) - net_income_margin
    never contributes a flag, since it has no grey zone; else passed.

    data_quality_flags (from collect_data_quality_flags) is passed through
    to the GateResult unchanged and has no effect on the status.
    """
    data_quality_flags = tuple(data_quality_flags)
    results = {
        "roic": roic,
        "net_debt_ebitda": net_debt_ebitda,
        "cash_conversion": cash_conversion,
        "net_income_margin": net_income_margin,
    }
    raw_values = {name: results[name].value for name in _METRIC_NAMES}

    invalid_reasons = tuple(
        results[name].invalid_reason for name in _METRIC_NAMES if results[name].invalid_reason is not None
    )
    # Only valid metrics are classified - an invalid one has no value and is
    # never a hard fail, flag or pass.
    classifications = {
        name: _CLASSIFIERS[name](raw_values[name], getattr(thresholds, name))
        for name in _METRIC_NAMES
        if results[name].invalid_reason is None
    }
    if invalid_reasons and not any(
        classifications.get(name) == FAILED for name in _HARD_FAIL_PRECEDENCE_METRICS
    ):
        return GateResult(
            status=NOT_EVALUABLE,
            flags=(),
            invalid_reasons=invalid_reasons,
            data_quality_flags=data_quality_flags,
            **raw_values,
        )

    flags = tuple(name for name in _METRIC_NAMES if classifications.get(name) == PASSED_WITH_FLAGS)

    if any(classification == FAILED for classification in classifications.values()):
        status = FAILED
    elif flags:
        status = PASSED_WITH_FLAGS
    else:
        status = PASSED

    return GateResult(
        status=status,
        flags=flags,
        invalid_reasons=invalid_reasons,
        data_quality_flags=data_quality_flags,
        **raw_values,
    )


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

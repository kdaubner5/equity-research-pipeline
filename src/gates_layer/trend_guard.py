"""Trend-Guard marks and result per docs/decisions.md ("Trend-Guard (Masterplan Stufe 1.1, Hebel 3 aus
Issue #52)", Abschnitt 2). Four signals, each computed and marked on its own; no exclusion, no
effect on any Stage-1 or Stage-2 status, flag count or rank (decision Q2: marks are shown only):

- T1 trend_revenue_falling: revenue last year / window mean strictly below revenue_ratio_flag_below
- T2 trend_margin_falling: EBITDA-margin ratio strictly below ebitda_margin_ratio_flag_below
- T3 trend_roic_below_floor: ROIC of the last year alone strictly below roic.fail_below (Stage 1)
- T4 trend_margin_peak: EBITDA-margin ratio strictly above ebitda_margin_ratio_flag_above

Result vocabulary (deliberately not passed/failed): trend_clear (all four computable, none marked),
trend_marked (at least one mark - a valid mark wins over a signal that cannot be computed, the reason
stays visible), trend_not_evaluable (a signal cannot be computed and nothing is marked).
"""

from dataclasses import dataclass
from fractions import Fraction
from typing import Optional, Tuple

from config_layer.thresholds import RoicThresholds
from config_layer.trend_guard import TrendGuardConfig
from metrics_layer.trend import ebitda_margin_ratio, revenue_ratio, roic_last_year, trend_window

STATUS_CLEAR = "trend_clear"
STATUS_MARKED = "trend_marked"
STATUS_NOT_EVALUABLE = "trend_not_evaluable"

MARK_REVENUE_FALLING = "trend_revenue_falling"
MARK_MARGIN_FALLING = "trend_margin_falling"
MARK_ROIC_BELOW_FLOOR = "trend_roic_below_floor"
MARK_MARGIN_PEAK = "trend_margin_peak"
# fixed order of the marks in every result, archive row and CSV line
MARK_ORDER = (MARK_REVENUE_FALLING, MARK_MARGIN_FALLING, MARK_ROIC_BELOW_FLOOR, MARK_MARGIN_PEAK)


@dataclass(frozen=True)
class TrendResult:
    status: str
    marks: Tuple[str, ...]
    revenue_ratio: Optional[float]
    ebitda_margin_ratio: Optional[float]
    roic_last_year: Optional[float]
    revenue_invalid_reason: Optional[str]
    margin_invalid_reason: Optional[str]
    roic_invalid_reason: Optional[str]

    @property
    def mark_count(self):
        return len(self.marks)


def not_evaluable_trend_result(reason) -> TrendResult:
    """A company whose data could not be used at all: every signal carries the same reason."""
    return TrendResult(
        status=STATUS_NOT_EVALUABLE, marks=(), revenue_ratio=None, ebitda_margin_ratio=None, roic_last_year=None,
        revenue_invalid_reason=reason, margin_invalid_reason=reason, roic_invalid_reason=reason,
    )


def _exact(threshold):
    # 0.9 means exactly 9/10, not the nearest binary float (same method as the Stage-2 percentile thresholds)
    return Fraction(str(threshold))


def evaluate_trend(
    periods, *, as_of_year, config: TrendGuardConfig, roic_thresholds: RoicThresholds, max_fiscal_year_lag
) -> TrendResult:
    """Evaluate the four signals of one company from the Stage-1 mapper's periods (fiscal_year, revenue,
    ebit, ebitda, equity, net_debt, total_assets, data_quality_flags). max_fiscal_year_lag is the Stage-2
    value (config/stage2.yaml fundamentals.max_fiscal_year_lag, decision Q8), read, not copied."""
    window, window_reason = trend_window(
        periods, as_of_year=as_of_year, max_fiscal_year_lag=max_fiscal_year_lag, config=config
    )
    if window is None:
        return not_evaluable_trend_result(window_reason)

    revenue = revenue_ratio(window)
    margin = ebitda_margin_ratio(window)
    roic = roic_last_year(window, thresholds=roic_thresholds)

    marked = set()
    if revenue.invalid_reason is None and revenue.value < _exact(config.revenue_ratio_flag_below):
        marked.add(MARK_REVENUE_FALLING)
    if margin.invalid_reason is None:
        if margin.value < _exact(config.ebitda_margin_ratio_flag_below):
            marked.add(MARK_MARGIN_FALLING)
        elif margin.value > _exact(config.ebitda_margin_ratio_flag_above):
            marked.add(MARK_MARGIN_PEAK)
    if roic.invalid_reason is None and roic.value < roic_thresholds.fail_below:
        marked.add(MARK_ROIC_BELOW_FLOOR)
    marks = tuple(m for m in MARK_ORDER if m in marked)

    if marks:
        status = STATUS_MARKED
    elif revenue.invalid_reason or margin.invalid_reason or roic.invalid_reason:
        status = STATUS_NOT_EVALUABLE
    else:
        status = STATUS_CLEAR
    return TrendResult(
        status=status,
        marks=marks,
        revenue_ratio=None if revenue.value is None else float(revenue.value),
        ebitda_margin_ratio=None if margin.value is None else float(margin.value),
        roic_last_year=roic.value,
        revenue_invalid_reason=revenue.invalid_reason,
        margin_invalid_reason=margin.invalid_reason,
        roic_invalid_reason=roic.invalid_reason,
    )

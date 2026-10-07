"""Stage-2 gates: relative valuation of the Stage-1 survivors per docs/decisions.md
("Stufe 2: Peer-Gruppen und relative Bewertung").

evaluate_stage2() takes the valuation inputs of the pre-filtered universe and the Stage-1
results, builds the peer base (every supported-profile company with a valid EV/EBITDA, whether
or not it survived Stage 1), and evaluates exactly the Stage-1 survivors: EV/EBITDA against
the peer group (or the absolute fallback), FCF-yield absolute. Combination follows Stage 1
(Phase 2.0d): a valid hard fail wins even if the other metric is invalid; otherwise any
invalid metric makes the company not_evaluable; otherwise any grey-zone result makes it
passed_with_flags. Informational marks (info_flags) never influence status, flags or rank.

Nothing is skipped silently: a survivor without valuation input, with an unsupported profile
or without a sector code gets a not_evaluable result with an explicit invalid_reason.
No I/O: the inputs are assembled by the caller (a runner, not part of this module).
"""

import dataclasses
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from config_layer.stage2 import Stage2Config
from gates_layer.peer_groups import LEVEL_ABSOLUTE, LEVEL_COARSE, PeerAssignment, assign_peers
from gates_layer.stage1_gates import FAILED, NOT_EVALUABLE, PASSED, PASSED_WITH_FLAGS, GateResult
from metrics_layer.plausibility import (
    SHARE_AMBIGUOUS,
    SHARE_OK,
    SHARE_SUSPECT,
    SHARE_UNCHECKED,
    classify_share_count_ratio,
    share_count_ratio,
)
from metrics_layer.result import MetricResult
from metrics_layer.valuation import (
    ev_ebitda_latest,
    ev_ebitda_normalized,
    fcf_yield_average,
    fcf_yield_incl_other_investing,
    net_inflow_fiscal_years,
    other_investing_triggered,
)

_METRIC_NAMES = ("ev_ebitda", "fcf_yield")

INFO_CYCLICAL_SECTOR = "cyclical_sector"
INFO_CAPEX_NET_INFLOW = "capex_net_inflow_treated_as_zero"
INFO_PEER_GROUP_COARSE = "peer_group_coarse"
INFO_PEER_GROUP_ABSOLUTE = "peer_group_absolute"
INFO_BORDERLINE_SECTOR = "borderline_sector"
INFO_SHARE_COUNT_UNCHECKED = "share_count_unchecked"
INFO_SHARE_COUNT_AMBIGUOUS = "share_count_ambiguous"

FLAG_FCF_YIELD_UNVERIFIED = "fcf_yield_unverified"

VALUATION_INPUT_MISSING = "valuation_input_missing"
PROFILE_UNSUPPORTED = "profile_unsupported"
NO_SECTOR_CODE = "no_sector_code"
SHARE_COUNT_SUSPECT = "share_count_suspect"


@dataclass(frozen=True)
class ValuationInput:
    """One company's Stage-2 input, assembled by the caller from already-loaded data.

    periods: the Stage-1 mapper's periods (fiscal_year, ebitda, net_debt, cfo,
    data_quality_flags). capex_by_year: data_layer.simfin_valuation.extract_capex_by_fiscal_year.
    market_cap_usd: universe_prefilter.market_cap_usd. profile_supported: from
    config/sector_profiles.yaml - an unsupported profile is never part of the peer base.
    borderline_sector: the profile config's informational marker, passed through.
    """

    ticker: str
    sector_code: Optional[int]
    market_cap_usd: Optional[float]
    periods: Sequence[dict]
    capex_by_year: Mapping[int, Optional[float]]
    profile_supported: bool = True
    borderline_sector: Optional[bool] = None
    # Plausibility inputs (docs/decisions.md "Stufe 2 - Datenplausibilitaet"): all optional - a caller
    # that has none gets share_count_unchecked and an unchecked counter-calculation, never a guess.
    # dividends_paid_by_year / other_investing_by_year: data_layer.simfin_valuation extractions (raw
    # cash-flow values by fiscal year); shares_outstanding / dividend_per_share: price-cache entry.
    dividends_paid_by_year: Mapping[int, Optional[float]] = field(default_factory=dict)
    other_investing_by_year: Mapping[int, Optional[float]] = field(default_factory=dict)
    shares_outstanding: Optional[float] = None
    dividend_per_share: Optional[float] = None


@dataclass(frozen=True)
class ShareCountAssessment:
    """Outcome of the dividend cross-check of one company's share count: verdict is one of
    SHARE_OK / SHARE_AMBIGUOUS / SHARE_SUSPECT / SHARE_UNCHECKED; ratio is R (None if unchecked);
    reason says why a company is unchecked."""

    verdict: str
    ratio: Optional[float]
    reason: Optional[str]


@dataclass(frozen=True)
class PeerBaseSummary:
    size: int
    excluded_share_count_suspect: Tuple[str, ...]  # tickers with an otherwise valid multiple, sorted


@dataclass(frozen=True)
class Stage2Result:
    """Stage-2 result for one Stage-1 survivor.

    status uses the Stage-1 vocabulary (passed, passed_with_flags, failed, not_evaluable).
    flags (grey-zone metrics) and invalid_reasons follow the fixed metric order ev_ebitda /
    fcf_yield; as in Stage 1, a "failed" company can carry invalid_reasons and a not_evaluable
    one never carries flags. Raw values are populated wherever the metric was valid and
    usable, whatever the status (a company without a sector code keeps ev_ebitda None: its
    multiple cannot be placed in any group). peer_* describe the EV/EBITDA comparison (None where it did not
    happen); peer_n_fine/peer_n_coarse are the valid-value counts that decided the level.
    ev_ebitda_normalized and its invalid reason are informational only. info_flags are
    informational marks in a fixed order and never influence status, flags or rank.
    """

    status: str
    flags: Tuple[str, ...]
    invalid_reasons: Tuple[str, ...]
    ev_ebitda: Optional[float]
    ev_ebitda_percentile: Optional[float]
    peer_level: Optional[str]
    peer_group: Optional[int]
    peer_rank: Optional[int]
    peer_n: Optional[int]
    peer_n_fine: Optional[int]
    peer_n_coarse: Optional[int]
    fcf_yield: Optional[float]
    ev_ebitda_normalized: Optional[float]
    ev_ebitda_normalized_invalid_reason: Optional[str]
    info_flags: Tuple[str, ...]
    # Plausibility results (docs/decisions.md "Stufe 2 - Datenplausibilitaet"): R of the dividend
    # cross-check; the FCF-yield with Other Investing counted and whether the ratio test fired
    # (None = not checkable). Defaults keep older constructors valid.
    share_count_ratio: Optional[float] = None
    fcf_yield_incl_other_investing: Optional[float] = None
    other_investing_triggered: Optional[bool] = None


def not_evaluable_result(reason) -> Stage2Result:
    """A not_evaluable Stage2Result carrying one explicit invalid_reason and no values - for a
    survivor whose input could not even be built (the runner names the cause)."""
    return Stage2Result(
        status=NOT_EVALUABLE,
        flags=(),
        invalid_reasons=(reason,),
        ev_ebitda=None,
        ev_ebitda_percentile=None,
        peer_level=None,
        peer_group=None,
        peer_rank=None,
        peer_n=None,
        peer_n_fine=None,
        peer_n_coarse=None,
        fcf_yield=None,
        ev_ebitda_normalized=None,
        ev_ebitda_normalized_invalid_reason=None,
        info_flags=(),
    )


def _classify_ev_ebitda_relative(percentile: Fraction, config: Stage2Config):
    """Peer percentile (0 = cheapest): at or below 1/peer_terciles clean, up to the grey-zone
    edge flag, above failed. Exact fractions - the grey edge 0.40 is exactly 2/5."""
    if percentile <= Fraction(1, config.ev_ebitda.peer_terciles):
        return PASSED
    if percentile <= Fraction(str(config.ev_ebitda.grey_zone_upper_percentile)):
        return PASSED_WITH_FLAGS
    return FAILED


def _classify_ev_ebitda_absolute(value, config: Stage2Config):
    if value < config.ev_ebitda.absolute_clean_pass_below:
        return PASSED
    if value >= config.ev_ebitda.absolute_fail_at_or_above:
        return FAILED
    return PASSED_WITH_FLAGS


def _classify_fcf_yield(value, config: Stage2Config):
    if value > config.fcf_yield.clean_pass_above:
        return PASSED
    if value < config.fcf_yield.fail_below:
        return FAILED
    return PASSED_WITH_FLAGS


def _assess_share_count(vi: ValuationInput, config: Stage2Config) -> ShareCountAssessment:
    """Dividend cross-check of one company: the dividends paid in the latest fiscal year against the
    share count and last dividend per share of the price cache. Unchecked (with the reason) if any
    input is missing or unusable - never silently ok."""
    if not vi.periods:
        return ShareCountAssessment(SHARE_UNCHECKED, None, "no_periods")
    latest_year = max(p["fiscal_year"] for p in vi.periods)
    result = share_count_ratio(vi.dividends_paid_by_year.get(latest_year), vi.shares_outstanding, vi.dividend_per_share)
    if result.invalid_reason is not None:
        return ShareCountAssessment(SHARE_UNCHECKED, None, result.invalid_reason)
    return ShareCountAssessment(classify_share_count_ratio(result.value, config), result.value, None)


def assess_share_counts(inputs: Mapping[str, ValuationInput], config: Stage2Config) -> Dict[str, ShareCountAssessment]:
    """Share-count assessment of every supported-profile input (unsupported profiles are never
    valued and therefore not assessed)."""
    return {t: _assess_share_count(vi, config) for t, vi in inputs.items() if vi.profile_supported}


def _peer_base_multiples(inputs, config, as_of_year):
    """(base, ev_results): base = ticker -> (sector_code, multiple) for every supported-profile
    company with a sector code and a valid EV/EBITDA; ev_results = the MetricResult of every
    such company (valid or not)."""
    base: Dict[str, Tuple[int, float]] = {}
    ev_results: Dict[str, MetricResult] = {}
    for ticker, vi in inputs.items():
        if not vi.profile_supported or vi.sector_code is None:
            continue
        result = ev_ebitda_latest(vi.periods, vi.market_cap_usd, as_of_year=as_of_year, config=config)
        if result.invalid_reason is None and _assess_share_count(vi, config).verdict == SHARE_SUSPECT:
            # a known-wrong share count means a wrong market cap and a wrong multiple: not a peer
            result = MetricResult(value=None, invalid_reason=SHARE_COUNT_SUSPECT)
        ev_results[ticker] = result
        if result.invalid_reason is None:
            base[ticker] = (vi.sector_code, result.value)
    return base, ev_results


def peer_base_size(inputs: Mapping[str, ValuationInput], *, as_of: date, config: Stage2Config) -> int:
    """Number of companies in the peer base (supported profile, sector code, valid EV/EBITDA)."""
    base, _ = _peer_base_multiples(inputs, config, as_of.year)
    return len(base)


def peer_base_summary(inputs: Mapping[str, ValuationInput], *, as_of: date, config: Stage2Config) -> PeerBaseSummary:
    """Size of the peer base and the companies that would be in it but for a suspect share count."""
    base, ev_results = _peer_base_multiples(inputs, config, as_of.year)
    excluded = sorted(t for t, r in ev_results.items() if r.invalid_reason == SHARE_COUNT_SUSPECT)
    return PeerBaseSummary(size=len(base), excluded_share_count_suspect=tuple(excluded))


def _evaluate_company(vi: ValuationInput, ev_result: MetricResult, base, *, as_of_year, config) -> Stage2Result:
    if not vi.profile_supported:
        return not_evaluable_result(PROFILE_UNSUPPORTED)

    share_check = _assess_share_count(vi, config)
    if share_check.verdict == SHARE_SUSPECT:
        # With a share count known to be wrong, the market cap and both metrics are unreliable: no
        # valuation is reported and no hard fail is derived from them (docs/decisions.md
        # "Stufe 2 - Datenplausibilitaet", Abschnitt 1). The ratio stays visible.
        return dataclasses.replace(not_evaluable_result(SHARE_COUNT_SUSPECT), share_count_ratio=share_check.ratio)

    assignment: Optional[PeerAssignment] = None
    if vi.sector_code is None:
        ev_result = MetricResult(value=None, invalid_reason=NO_SECTOR_CODE)
    elif ev_result.invalid_reason is None:
        assignment = assign_peers(vi.ticker, vi.sector_code, ev_result.value, base, config.peer_groups)

    fcf_result = fcf_yield_average(
        vi.periods, vi.capex_by_year, vi.market_cap_usd, as_of_year=as_of_year, config=config
    )
    normalized = ev_ebitda_normalized(vi.periods, vi.market_cap_usd, as_of_year=as_of_year, config=config)

    # Counter-calculation of the investment line (Abschnitt 2): only meaningful for a valid main yield.
    other_triggered = None
    alt_yield = None
    alt_invalid = False
    if fcf_result.invalid_reason is None:
        other_triggered = other_investing_triggered(vi.periods, vi.capex_by_year, vi.other_investing_by_year, config=config)
        if other_triggered is not None:
            alt = fcf_yield_incl_other_investing(
                vi.periods, vi.capex_by_year, vi.other_investing_by_year, vi.market_cap_usd,
                as_of_year=as_of_year, config=config,
            )
            alt_yield, alt_invalid = alt.value, alt.invalid_reason is not None

    classifications = {}
    if assignment is not None:
        if assignment.level == LEVEL_ABSOLUTE:
            classifications["ev_ebitda"] = _classify_ev_ebitda_absolute(ev_result.value, config)
        else:
            classifications["ev_ebitda"] = _classify_ev_ebitda_relative(assignment.percentile, config)
    if fcf_result.invalid_reason is None:
        classifications["fcf_yield"] = _classify_fcf_yield(fcf_result.value, config)

    metric_results = {"ev_ebitda": ev_result, "fcf_yield": fcf_result}
    invalid_reasons = tuple(
        metric_results[name].invalid_reason for name in _METRIC_NAMES if metric_results[name].invalid_reason is not None
    )
    flags: Tuple[str, ...] = ()
    if invalid_reasons and FAILED not in classifications.values():
        status = NOT_EVALUABLE
    else:
        flags = tuple(name for name in _METRIC_NAMES if classifications.get(name) == PASSED_WITH_FLAGS)
        if FAILED in classifications.values():
            status = FAILED
        elif flags:
            status = PASSED_WITH_FLAGS
        else:
            status = PASSED
        # A clean main yield that the counter-calculation cannot confirm is flagged - never failed or
        # excluded (the content of Other Investing is not known). A failed or not_evaluable company
        # stays what it is; a main yield already in the grey zone needs no extra flag.
        if (
            status in (PASSED, PASSED_WITH_FLAGS)
            and classifications.get("fcf_yield") == PASSED
            and other_triggered is True
            and (alt_invalid or _classify_fcf_yield(alt_yield, config) != PASSED)
        ):
            flags = flags + (FLAG_FCF_YIELD_UNVERIFIED,)
            status = PASSED_WITH_FLAGS

    info_flags: List[str] = []
    if vi.sector_code in config.cyclical_sectors.sector_codes:
        info_flags.append(INFO_CYCLICAL_SECTOR)
    if fcf_result.invalid_reason is None and net_inflow_fiscal_years(vi.periods, vi.capex_by_year, config=config):
        info_flags.append(INFO_CAPEX_NET_INFLOW)
    if assignment is not None and assignment.level == LEVEL_COARSE:
        info_flags.append(INFO_PEER_GROUP_COARSE)
    if assignment is not None and assignment.level == LEVEL_ABSOLUTE:
        info_flags.append(INFO_PEER_GROUP_ABSOLUTE)
    if vi.borderline_sector:
        info_flags.append(INFO_BORDERLINE_SECTOR)
    if share_check.verdict == SHARE_UNCHECKED:
        info_flags.append(INFO_SHARE_COUNT_UNCHECKED)
    elif share_check.verdict == SHARE_AMBIGUOUS:
        info_flags.append(INFO_SHARE_COUNT_AMBIGUOUS)

    return Stage2Result(
        status=status,
        flags=flags,
        invalid_reasons=invalid_reasons,
        ev_ebitda=ev_result.value,
        ev_ebitda_percentile=None if assignment is None or assignment.percentile is None else float(assignment.percentile),
        peer_level=None if assignment is None else assignment.level,
        peer_group=None if assignment is None else assignment.group_key,
        peer_rank=None if assignment is None else assignment.rank,
        peer_n=None if assignment is None else assignment.n,
        peer_n_fine=None if assignment is None else assignment.n_fine,
        peer_n_coarse=None if assignment is None else assignment.n_coarse,
        fcf_yield=fcf_result.value,
        ev_ebitda_normalized=normalized.value,
        ev_ebitda_normalized_invalid_reason=normalized.invalid_reason,
        info_flags=tuple(info_flags),
        share_count_ratio=share_check.ratio,
        fcf_yield_incl_other_investing=alt_yield,
        other_investing_triggered=other_triggered,
    )


def evaluate_stage2(
    inputs: Mapping[str, ValuationInput],
    stage1_results: Mapping[str, GateResult],
    *,
    as_of: date,
    config: Stage2Config,
) -> Dict[str, Stage2Result]:
    """Stage-2 results for exactly the Stage-1 survivors (status passed / passed_with_flags).

    inputs maps ticker -> ValuationInput for the pre-filtered universe (the key must equal the
    input's ticker); stage1_results holds the Stage-1 GateResults. The peer base is built from
    all inputs, independent of the Stage-1 status. Deterministic: the result does not depend
    on the order of either mapping.
    """
    for key, vi in inputs.items():
        if vi.ticker != key:
            raise ValueError(f"input key {key!r} does not match its ticker {vi.ticker!r}")

    as_of_year = as_of.year
    base, ev_results = _peer_base_multiples(inputs, config, as_of_year)

    results: Dict[str, Stage2Result] = {}
    survivors = sorted(t for t, r in stage1_results.items() if r.status in (PASSED, PASSED_WITH_FLAGS))
    for ticker in survivors:
        vi = inputs.get(ticker)
        if vi is None:
            results[ticker] = not_evaluable_result(VALUATION_INPUT_MISSING)
            continue
        results[ticker] = _evaluate_company(
            vi, ev_results.get(ticker, MetricResult(value=None, invalid_reason=NO_SECTOR_CODE)), base,
            as_of_year=as_of_year, config=config,
        )
    return results


def rank_stage2(
    stage1_results: Mapping[str, GateResult], stage2_results: Mapping[str, Stage2Result]
) -> List[Tuple[str, Stage2Result]]:
    """Rank the companies that passed Stage 2 (passed / passed_with_flags).

    Order: total flag count (Stage 1 + Stage 2) ascending, then EV/EBITDA peer percentile
    ascending (cheaper within its group first; companies without a percentile - absolute
    level - after all with one), then ticker. No composite score, same philosophy as the
    Stage-1 rank. failed / not_evaluable companies are absent from the result. A Stage-2
    result without a Stage-1 result is a caller error.
    """
    eligible = []
    for ticker, result in stage2_results.items():
        if result.status not in (PASSED, PASSED_WITH_FLAGS):
            continue
        if ticker not in stage1_results:
            raise ValueError(f"{ticker!r} has a Stage-2 result but no Stage-1 result")
        percentile = result.ev_ebitda_percentile
        key = (
            len(stage1_results[ticker].flags) + len(result.flags),
            percentile is None,
            0.0 if percentile is None else percentile,
            ticker,
        )
        eligible.append((key, ticker, result))
    return [(ticker, result) for _, ticker, result in sorted(eligible, key=lambda item: item[0])]

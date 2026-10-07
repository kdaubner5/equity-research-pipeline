"""Shared builders for the Stage-2 tests (docs/decisions.md "Stufe 2: Peer-Gruppen
und relative Bewertung"). Artificial data only - no data/, no database, no network.

make_config() builds a Stage2Config directly instead of loading config/stage2.yaml,
so the boundary tests do not break when a start value in the real file is
recalibrated (the real file has its own tests in test_stage2_config.py).
"""

import dataclasses

from config_layer.stage2 import (
    CyclicalSectorsConfig,
    EvEbitdaConfig,
    FcfYieldConfig,
    FundamentalsConfig,
    OtherInvestingCheckConfig,
    PeerGroupsConfig,
    ShareCountCheckConfig,
    Stage2Config,
)


def make_config(**overrides) -> Stage2Config:
    """Test config: min_size 8, divisor 1000, terciles 3, grey zone up to 0.40,
    absolute 8.0/10.0, FCF-yield 6 %/5 % over 3 years, lag 1, cyclicals {107002},
    share count suspect above R 6.0 (monthly band 9-15), other investing ratio 2.0.

    overrides: group name -> dict of fields to replace, e.g.
    make_config(peer_groups={"min_size": 3}).
    """
    groups = {
        "peer_groups": PeerGroupsConfig(min_size=8, coarse_level_divisor=1000),
        "ev_ebitda": EvEbitdaConfig(
            implausible_above=100.0,
            peer_terciles=3,
            grey_zone_upper_percentile=0.40,
            absolute_clean_pass_below=8.0,
            absolute_fail_at_or_above=10.0,
            normalized_ebitda_years=3,
        ),
        "fcf_yield": FcfYieldConfig(years=3, clean_pass_above=0.06, fail_below=0.05, implausible_above=0.50),
        "fundamentals": FundamentalsConfig(max_fiscal_year_lag=1),
        "cyclical_sectors": CyclicalSectorsConfig(sector_codes=(107002,)),
        "share_count_check": ShareCountCheckConfig(suspect_ratio_above=6.0, monthly_band_low=9.0, monthly_band_high=15.0),
        "other_investing_check": OtherInvestingCheckConfig(min_ratio_to_capex=2.0),
    }
    for name, fields in overrides.items():
        groups[name] = dataclasses.replace(groups[name], **fields)
    return Stage2Config(**groups)


def period(fiscal_year, *, ebitda=100.0, net_debt=200.0, cfo=100.0, flags=()):
    """The period keys the Stage-2 metrics read (same shape as the Stage-1 mapper's output)."""
    return {
        "fiscal_year": fiscal_year,
        "ebitda": ebitda,
        "net_debt": net_debt,
        "cfo": cfo,
        "data_quality_flags": list(flags),
    }


def three_periods(*, ebitda=100.0, net_debt=200.0, cfo=100.0, last_year=2025):
    """Three consecutive years ending at last_year, identical figures."""
    return [period(last_year - 2 + i, ebitda=ebitda, net_debt=net_debt, cfo=cfo) for i in range(3)]


def capex_for(periods, value=-30.0):
    """A capex map (fiscal_year -> Change in Fixed Assets & Intangibles) with one value for every period."""
    return {p["fiscal_year"]: value for p in periods}

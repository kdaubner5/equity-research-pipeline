"""Shared builders for the Trend-Guard tests (docs/decisions.md "Trend-Guard (Masterplan Stufe 1.1,
Hebel 3 aus Issue #52)"). Artificial data and the committed pilot fixtures only - no data/, no
database, no network."""

import dataclasses
import json
from pathlib import Path

from config_layer.thresholds import RoicThresholds
from config_layer.trend_guard import TrendGuardConfig

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "companies"

# the real config/thresholds.yaml values at the time of writing, written out so a boundary test does
# not move when thresholds.yaml is recalibrated (the real file has its own tests)
ROIC = RoicThresholds(nopat_tax_rate=0.25, ic_asset_floor_ratio=0.1, implausible_above=0.6, clean_pass_above=0.12, fail_below=0.1)


def make_config(**overrides) -> TrendGuardConfig:
    """Test config: window 3 years, at least 3 valid years, flag below 0.90 (revenue, EBITDA margin), above 1.25 (margin peak)."""
    fields = dict(
        years=3, min_years=3, revenue_ratio_flag_below=0.90, ebitda_margin_ratio_flag_below=0.90,
        ebitda_margin_ratio_flag_above=1.25,
    )
    fields.update(overrides)
    return TrendGuardConfig(**fields)


def fixture_periods(company_id):
    with open(FIXTURES_DIR / f"{company_id}.json", encoding="utf-8") as f:
        return json.load(f)["periods"]


def period(fiscal_year, *, revenue=100.0, ebit=20.0, ebitda=30.0, equity=150.0, net_debt=50.0, total_assets=300.0, flags=()):
    """The period keys the Trend-Guard reads (same shape as the Stage-1 mapper's output)."""
    return {
        "fiscal_year": fiscal_year, "revenue": revenue, "ebit": ebit, "ebitda": ebitda, "equity": equity,
        "net_debt": net_debt, "total_assets": total_assets, "data_quality_flags": list(flags),
    }


def periods_from(series, *, last_year=2025, **fixed):
    """series: list of dicts of per-year overrides, oldest first, ending at last_year."""
    n = len(series)
    return [period(last_year - n + 1 + i, **{**fixed, **values}) for i, values in enumerate(series)]


def replace_roic(**fields):
    return dataclasses.replace(ROIC, **fields)

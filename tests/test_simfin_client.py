import json
from pathlib import Path

import pytest

from data_layer.simfin_client import (
    DEBT_COMPONENT_MISSING_FLAG,
    DEBT_STATEMENT_MISMATCH_FLAG,
    NET_DEBT_UNRELIABLE_FLAG,
    SimFinMappingError,
    build_company_record,
    map_simfin_to_periods,
)

SIMFIN_RAW_DIR = Path(__file__).parent / "fixtures" / "simfin_raw"


def _load_raw(ticker):
    with open(SIMFIN_RAW_DIR / f"{ticker}.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize(
    "ticker",
    ["CVGI", "SMP", "FOSL", "GENC", "USNA", "SCSC", "DXPE", "PLOW", "CENT", "ATRO"],
)
def test_real_companies_produce_complete_plausible_records(ticker):
    record = build_company_record(ticker, _load_raw(ticker))

    assert record["company_id"] == ticker
    assert record["currency"] == "USD"
    assert record["sector"] is None
    assert record["name"]

    periods = record["periods"]
    assert len(periods) == 3
    fiscal_years = [p["fiscal_year"] for p in periods]
    assert fiscal_years == sorted(fiscal_years)
    assert len(fiscal_years) == len(set(fiscal_years))

    for period in periods:
        for field in ("revenue", "ebit", "ebitda", "equity", "total_assets", "net_income", "cfo"):
            assert isinstance(period[field], (int, float))
        assert period["tax_rate"] is None
        assert period["net_debt"] is None or isinstance(period["net_debt"], (int, float))
        assert isinstance(period["data_quality_flags"], list)


def test_tax_rate_is_always_none_no_mapping_effort():
    # docs/decisions.md: tax_rate gets no SimFin mapping - no Stage-1 metric
    # still reads it (ROIC's fixed 25% rate since masterplan v1.1).
    periods = map_simfin_to_periods(_load_raw("ATRO"))
    assert all(p["tax_rate"] is None for p in periods)


def test_total_assets_mapped_directly_from_bs():
    # ATRO FY2025: BS "Total Assets" column, Q4 row - direct field, no
    # fallback/aggregation needed (docs/decisions.md).
    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("ATRO"))}
    assert periods[2025]["total_assets"] == 706_678_000
    assert periods[2024]["total_assets"] == 648_764_000
    assert periods[2023]["total_assets"] == 633_792_000


def test_fiscal_year_anchor_uses_companys_own_most_recent_years():
    # SGC's PL data ends at FY2023 while its BS/CF extend to FY2025 - the
    # anchor is "the 3 most recent years THIS company's PL/EBIT has", not
    # "the 3 most recent calendar years" - so SGC still yields exactly 3
    # complete periods (2021/2022/2023), not insufficient_years.
    #
    # Correction to an assumption in the task for this branch: SGC was
    # expected to resolve via insufficient_years, analogous to CATO. It
    # does not - all required CF/BS fields are present for exactly its 3
    # most-recent EBIT-having years (docs/decisions.md, "SimFin-Client"
    # Entscheidung 7). Forcing that outcome would require a PL-staleness-
    # vs-BS/CF special mechanism the task explicitly says is not needed.
    periods = map_simfin_to_periods(_load_raw("SGC"))
    assert [p["fiscal_year"] for p in periods] == [2021, 2022, 2023]


@pytest.mark.parametrize(
    "ticker,expected_years",
    [
        ("CATO", [2019, 2020]),  # only 2 PL/EBIT years exist at all
        ("MATW", [2023, 2024]),  # FY2025 dropped: missing total_assets/cash
        ("KELYA", []),  # all 3 candidate years dropped: D&A missing throughout
    ],
)
def test_insufficient_years_cases_return_fewer_than_3_periods(ticker, expected_years):
    periods = map_simfin_to_periods(_load_raw(ticker))
    assert [p["fiscal_year"] for p in periods] == expected_years
    assert len(periods) < 3


def test_matw_fy2025_dropped_for_missing_total_assets_and_cash_not_crashed():
    # Building the whole record must not raise just because one candidate
    # year lacks a non-defaultable field (docs/decisions.md Entscheidung 5,
    # a deliberate difference from edgar_client.py's per-field crash).
    record = build_company_record("MATW", _load_raw("MATW"))
    assert [p["fiscal_year"] for p in record["periods"]] == [2023, 2024]


def test_kelya_all_years_dropped_returns_empty_periods_not_error():
    record = build_company_record("KELYA", _load_raw("KELYA"))
    assert record["periods"] == []


@pytest.mark.parametrize(
    "ticker,fiscal_year,expected_net_debt",
    [
        ("USNA", 2025, None),  # ratio 9,000,000/83,424,000 = 10.79% >= 10%
        ("CATO", 2020, None),  # EBITDA = -79,249,000 + 14,681,000 = -64,568,000 <= 0
    ],
)
def test_net_debt_unreliable_fires_at_or_above_10_percent_or_non_positive_ebitda(
    ticker, fiscal_year, expected_net_debt
):
    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw(ticker))}
    period = periods[fiscal_year]
    assert period["net_debt"] == expected_net_debt
    assert NET_DEBT_UNRELIABLE_FLAG in period["data_quality_flags"]


@pytest.mark.parametrize(
    "ticker,fiscal_year,expected_net_debt",
    [
        ("MATW", 2023, 748_079_000),  # ratio 18,224,000/184,647,000 = 9.87% - just under 10%
        ("CENT", 2025, 309_215_000),  # ratio 231,000/334,936,000 = 0.07% - clearly under
    ],
)
def test_net_debt_unreliable_stays_off_below_10_percent(ticker, fiscal_year, expected_net_debt):
    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw(ticker))}
    period = periods[fiscal_year]
    assert period["net_debt"] == expected_net_debt
    assert NET_DEBT_UNRELIABLE_FLAG not in period["data_quality_flags"]
    # both cases have a nonzero debt cash flow despite the small ratio
    assert DEBT_STATEMENT_MISMATCH_FLAG in period["data_quality_flags"]


@pytest.mark.parametrize(
    "ticker,fiscal_year",
    [
        ("LAKE", 2023),  # ratio 1,791,000/8,103,000 = 22.1%
        ("LAKE", 2024),  # ratio 15,295,000/4,589,000 = 333.3%
        ("LAKE", 2025),  # EBITDA = -8,601,000 <= 0
        ("CULP", 2024),  # EBITDA = -10,672,000 + 6,911,000 = -3,761,000 <= 0, despite a tiny debt_cf of 20,000
    ],
)
def test_lake_and_culp_fy2024_are_not_actually_under_10_percent_examples(ticker, fiscal_year):
    # Correction to an assumption in the task for this branch: LAKE and
    # CULP's FY2024 were expected to demonstrate net_debt_unreliable
    # staying OFF (ratio < 10%). Checked against the real committed
    # fixtures, neither holds up - LAKE has no year under the 10%
    # threshold at all (all 3 of its anchored years are unreliable, for a
    # mix of ratio and non-positive-EBITDA reasons), and CULP FY2024's
    # EBITDA is itself negative, which triggers unreliability regardless
    # of its debt cash flow being tiny (20,000). See docs/decisions.md,
    # "SimFin-Client" Entscheidung 7, and
    # test_net_debt_unreliable_stays_off_below_10_percent above for the
    # verified replacements (MATW FY2023, CENT).
    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw(ticker))}
    period = periods[fiscal_year]
    assert period["net_debt"] is None
    assert NET_DEBT_UNRELIABLE_FLAG in period["data_quality_flags"]


def test_debt_component_missing_defaults_to_zero_and_flags_it():
    # ATRO: Short Term Debt is null in FY2024/FY2025 but present in FY2023 -
    # same fixture isolates both the "missing, defaulted" and the "fully
    # reported, no flag" branch.
    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("ATRO"))}

    assert periods[2025]["net_debt"] == 334_451_000 - 18_180_000
    assert DEBT_COMPONENT_MISSING_FLAG in periods[2025]["data_quality_flags"]
    assert periods[2024]["net_debt"] == 168_669_000 - 9_285_000
    assert DEBT_COMPONENT_MISSING_FLAG in periods[2024]["data_quality_flags"]

    assert periods[2023]["net_debt"] == (8_996_000 + 159_237_000) - 4_756_000
    assert DEBT_COMPONENT_MISSING_FLAG not in periods[2023]["data_quality_flags"]


def test_debt_component_missing_and_net_debt_unreliable_can_combine():
    # CATO FY2020: both Short Term Debt AND Long Term Debt are null, AND
    # EBITDA is negative - both flags fire together, net_debt stays None
    # (the unreliability guard, not the debt-component default, decides
    # net_debt's final value).
    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("CATO"))}
    period = periods[2020]
    assert period["net_debt"] is None
    assert DEBT_COMPONENT_MISSING_FLAG in period["data_quality_flags"]
    assert NET_DEBT_UNRELIABLE_FLAG in period["data_quality_flags"]


def test_da_sourced_from_cf_not_pl():
    # docs/decisions.md Entscheidung 3: MATW's PL "Depreciation & Amortization"
    # is negative and much smaller in magnitude than CF's (FY2024: PL =
    # -37,023,000 vs CF = 94,770,000) - using PL's figure would silently
    # produce a wrong (and wrong-signed) EBITDA.
    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("MATW"))}
    # FY2023: ebit=88,117,000, CF D&A=96,530,000 -> ebitda=184,647,000
    assert periods[2023]["ebitda"] == 88_117_000 + 96_530_000


def test_missing_pl_statement_raises_mapping_error():
    with pytest.raises(SimFinMappingError):
        map_simfin_to_periods({"statements": []})


def test_no_ebit_data_at_all_raises_mapping_error():
    tags = {
        "statements": [
            {
                "statement": "PL",
                "columns": ["Fiscal Period", "Fiscal Year", "Operating Income (Loss)", "Revenue", "Net Income"],
                "data": [["FY", 2024, None, 1000, 60]],
            }
        ]
    }
    with pytest.raises(SimFinMappingError):
        map_simfin_to_periods(tags)


def test_build_company_record_rejects_non_usd_currency():
    with pytest.raises(SimFinMappingError):
        build_company_record("ATRO", _load_raw("ATRO"), currency="EUR")


def test_fewer_than_max_years_requested_returns_only_requested_count():
    periods = map_simfin_to_periods(_load_raw("ATRO"), max_years=1)
    assert [p["fiscal_year"] for p in periods] == [2025]

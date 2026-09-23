import copy
import json
from pathlib import Path

import pytest

from conftest import FakeUrlopenResponse
from data_layer import simfin_client
from data_layer.simfin_client import (
    DEBT_COMPONENT_MISSING_FLAG,
    DEBT_DATA_COMPLETELY_MISSING_FLAG,
    DEBT_STOCK_FLOW_MISMATCH_FLAG,
    DEBT_STOCK_FLOW_UNVERIFIABLE_FLAG,
    NET_DEBT_UNRELIABLE_FLAG,
    PL_LAGS_OTHER_STATEMENTS_FLAG,
    REQUEST_TIMEOUT_SECONDS,
    YEAR_DROPPED_MISSING_FIELD_FLAG,
    SimFinMappingError,
    _debt_stock_flow_reconciles,
    build_company_record,
    fetch_companies_list,
    fetch_statements_batch,
    map_simfin_to_periods,
    resolve_simfin_id,
)
from gates_layer.stage1_gates import NOT_EVALUABLE, evaluate_stage1_gates
from metrics_layer.cash_conversion import cash_conversion_3yr_average, net_income_margin_3yr_average
from metrics_layer.net_debt_ebitda import net_debt_ebitda_latest
from metrics_layer.roic import roic_3yr_average

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
    # Entscheidung 7). The PL-lags-BS/CF discrepancy is made visible by an
    # informative flag instead (Entscheidung 11, tests below), not by an
    # invalid_reason.
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
        # dBS = change in balance-sheet debt Q4(t) vs Q4(t-1); CF = reported debt cash flow.
        ("ROCK", 2023, -99_426_000),  # BS debt null after a full repayment: dBS -88,762,000 vs CF -91,000,000 (2.5% of debt level)
        ("UFPT", 2023, 26_737_000),  # dBS -23,000,000 vs CF -23,063,000 - 31.7% of EBITDA, but the two statements agree
        ("UFPT", 2024, 175_925_000),  # dBS +157,375,000 vs CF +157,302,000 - 158.6% of EBITDA, agrees to 0.05%
        ("UFPT", 2025, 115_154_000),  # dBS -53,920,000 vs CF -53,983,000
        ("USNA", 2025, -144_380_000),  # dBS -9,000,000 vs CF -9,000,000
    ],
)
def test_net_debt_reliable_when_balance_sheet_and_cash_flow_reconcile_regardless_of_size_vs_ebitda(
    ticker, fiscal_year, expected_net_debt
):
    # docs/decisions.md "net_debt-Zuverlässigkeit - Stock-Flow-Abgleich",
    # Entscheidung 1: the old rule (|debt cash flow| / EBITDA >= 10%) rejected
    # every one of these years for being a big movement, although nothing
    # contradicted anything.
    period = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw(ticker))}[fiscal_year]
    assert period["net_debt"] == expected_net_debt
    assert NET_DEBT_UNRELIABLE_FLAG not in period["data_quality_flags"]
    assert DEBT_STOCK_FLOW_MISMATCH_FLAG not in period["data_quality_flags"]


@pytest.mark.parametrize(
    "ticker,fiscal_year",
    [
        ("LAKE", 2023),  # dBS +624,000 vs CF -1,791,000: opposite signs, 235% of the debt level, 1.57% of total assets
        ("FOSL", 2025),  # dBS +12,988,000 vs CF +35,414,000: 12.6% of the debt level
        ("ATRO", 2025),  # dBS +165,782,000 with NO reported debt cash flow (null counts as 0)
    ],
)
def test_net_debt_unreliable_when_balance_sheet_and_cash_flow_contradict(ticker, fiscal_year):
    period = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw(ticker))}[fiscal_year]
    assert period["net_debt"] is None
    assert DEBT_STOCK_FLOW_MISMATCH_FLAG in period["data_quality_flags"]
    assert NET_DEBT_UNRELIABLE_FLAG in period["data_quality_flags"]
    assert DEBT_STOCK_FLOW_UNVERIFIABLE_FLAG not in period["data_quality_flags"]


def test_reconciliation_tolerance_is_bracketed_by_real_data_around_5_percent_of_debt_level():
    # No natural gap exists in the 23 pilots (docs/decisions.md, Entscheidung
    # 2) - these two real cases just pin which side of the chosen 5% each
    # falls on, so a tolerance change is a visible, deliberate act.
    cvgi_2025 = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("CVGI"))}[2025]
    assert cvgi_2025["net_debt"] == 73_093_000  # dBS -29,125,000 vs CF -22,707,000: 4.7% - inside
    lake_2024 = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("LAKE"))}[2024]
    assert lake_2024["net_debt"] is None  # dBS +16,336,000 vs CF +15,295,000: 6.0% - just outside
    assert DEBT_STOCK_FLOW_MISMATCH_FLAG in lake_2024["data_quality_flags"]


def test_absolute_floor_keeps_a_tiny_cash_flow_at_zero_debt_reliable():
    # CULP FY2024: no balance-sheet debt in either year, but a reported debt
    # cash flow of +20,000 - 0.015% of total assets. A purely relative
    # tolerance would be degenerate at debt level 0 (Entscheidung 2).
    period = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("CULP"))}[2024]
    assert period["net_debt"] == -10_012_000
    assert NET_DEBT_UNRELIABLE_FLAG not in period["data_quality_flags"]


@pytest.mark.parametrize(
    "ticker,fiscal_year,expected_net_debt",
    [
        ("CATO", 2020, -17_510_000),  # EBITDA = -79,249,000 + 14,681,000 = -64,568,000
        ("LAKE", 2025, 19_758_000),  # EBITDA = -8,601,000
        ("CULP", 2024, -10_012_000),  # EBITDA = -3,761,000
        ("CULP", 2025, 7_085_000),  # EBITDA = -4,793,000
    ],
)
def test_non_positive_ebitda_does_not_make_net_debt_unreliable(ticker, fiscal_year, expected_net_debt):
    # Entscheidung 3: whether net_debt is right does not depend on EBITDA's
    # sign - the old rule coupled two independent questions.
    period = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw(ticker))}[fiscal_year]
    assert period["ebitda"] <= 0
    assert period["net_debt"] == expected_net_debt
    assert NET_DEBT_UNRELIABLE_FLAG not in period["data_quality_flags"]


def test_lake_window_mixes_unreliable_and_reliable_years():
    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("LAKE"))}
    assert sorted(periods) == [2023, 2024, 2025]
    assert periods[2023]["net_debt"] is None
    assert periods[2024]["net_debt"] is None
    assert periods[2025]["net_debt"] == 19_758_000  # EBITDA <= 0 no longer matters


def test_missing_prior_year_balance_sheet_is_unverifiable_and_unreliable():
    # No BS Q4 row for t-1 -> the reconciliation can neither find nor rule
    # out a contradiction. UFPT with its FY2022 BS row removed: FY2023 loses
    # its prior year, FY2024 (prior year 2023 still present) is unaffected.
    raw = copy.deepcopy(_load_raw("UFPT"))
    bs = next(s for s in raw["statements"] if s["statement"] == "BS")
    fp_idx, fy_idx = bs["columns"].index("Fiscal Period"), bs["columns"].index("Fiscal Year")
    bs["data"] = [row for row in bs["data"] if not (row[fp_idx] == "Q4" and row[fy_idx] == 2022)]

    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(raw)}
    assert periods[2023]["net_debt"] is None
    assert DEBT_STOCK_FLOW_UNVERIFIABLE_FLAG in periods[2023]["data_quality_flags"]
    assert NET_DEBT_UNRELIABLE_FLAG in periods[2023]["data_quality_flags"]
    assert DEBT_STOCK_FLOW_MISMATCH_FLAG not in periods[2023]["data_quality_flags"]
    assert periods[2024]["net_debt"] == 175_925_000


def test_prior_year_debt_component_missing_defaults_to_zero_and_flags_it():
    # Code review of this branch: the reconciliation's own baseline (year
    # t-1) can have a missing debt component just like year t - silently
    # defaulting it to 0 without a flag would make the check look more
    # certain than it is than the symmetric, already-flagged current-year
    # case. UFPT FY2023's Short Term Debt (4,000,000 in the real fixture) is
    # nulled out here to isolate the prior-year case: FY2024's OWN components
    # are both fully known (no flag from that side). net_debt is unaffected -
    # the reconciliation still holds even under the wrong 0-default
    # (discrepancy 4,073,000 vs. a 9,468,750 tolerance) - which is exactly why
    # a flag, not a null, is the right signal here.
    raw = copy.deepcopy(_load_raw("UFPT"))
    bs = next(s for s in raw["statements"] if s["statement"] == "BS")
    fp_idx, fy_idx = bs["columns"].index("Fiscal Period"), bs["columns"].index("Fiscal Year")
    st_idx = bs["columns"].index("Short Term Debt")
    for row in bs["data"]:
        if row[fp_idx] == "Q4" and row[fy_idx] == 2023:
            row[st_idx] = None

    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(raw)}
    assert periods[2024]["net_debt"] == 175_925_000
    assert DEBT_COMPONENT_MISSING_FLAG in periods[2024]["data_quality_flags"]
    assert NET_DEBT_UNRELIABLE_FLAG not in periods[2024]["data_quality_flags"]


def test_reconciliation_tolerance_boundaries():
    # relative part: 5% of max(debt_t, debt_t-1); total_assets small enough
    # that the absolute floor (0.1%) is not the binding term here
    assert _debt_stock_flow_reconciles(1000, 800, 150, total_assets=1000)  # diff 50 == 5% of 1000: inclusive
    assert not _debt_stock_flow_reconciles(1000, 800, 149, total_assets=1000)  # diff 51
    # symmetric in the debt level: debt fully repaid, level taken from t-1
    assert _debt_stock_flow_reconciles(0, 1000, -950, total_assets=1000)  # diff 50 == 5% of 1000
    assert not _debt_stock_flow_reconciles(0, 1000, -940, total_assets=1000)  # diff 60
    # absolute floor: 0.1% of total assets, but ONLY when the debt level is
    # exactly 0 - not as an unconditional alternative to the relative term
    # (code review of this branch: the floor used to win via max() whenever it
    # was looser than the relative tolerance, regardless of whether debt was
    # actually near zero).
    assert _debt_stock_flow_reconciles(0, 0, 0, total_assets=1_000_000)  # exact zero always reconciles
    assert _debt_stock_flow_reconciles(0, 0, 1000, total_assets=1_000_000)  # diff 1000 == 0.1% of 1,000,000
    assert not _debt_stock_flow_reconciles(0, 0, 1001, total_assets=1_000_000)


def test_absolute_floor_does_not_rescue_a_real_contradiction_at_nonzero_debt():
    # Code review of this branch, reproduced from the real KELYA FY2020
    # fixture: debt fell from 1,900,000 to 300,000 (delta -1,600,000) against
    # a reported debt cash flow of -3,700,000 - a 2,100,000 discrepancy, 110%
    # of the debt level, a clear stock-flow contradiction. Debt is small only
    # relative to this company's 2,561,900,000 total assets, not actually
    # zero, so the assets floor (2,561,900) must NOT apply here - only the 5%
    # relative tolerance (95,000) is relevant, and the discrepancy fails it.
    assert not _debt_stock_flow_reconciles(300_000, 1_900_000, -3_700_000, total_assets=2_561_900_000)


def test_kelya_fy2020_no_longer_silently_reconciled_by_the_assets_floor():
    # End-to-end version of the fix above, against the real fixture. FY2020
    # falls outside KELYA's default 3-year anchor window (it is dropped from
    # the pilot gates for unrelated reasons - see test_simfin_pilot_gates.py),
    # so this exercises the mapping directly with a wider window.
    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("KELYA"), max_years=10)}
    assert periods[2020]["net_debt"] is None
    assert DEBT_STOCK_FLOW_MISMATCH_FLAG in periods[2020]["data_quality_flags"]
    assert NET_DEBT_UNRELIABLE_FLAG in periods[2020]["data_quality_flags"]


def test_debt_component_missing_defaults_to_zero_and_flags_it():
    # ATRO: Short Term Debt is null in FY2024/FY2025 but present in FY2023 -
    # same fixture isolates the "missing, defaulted" and the "fully
    # reported, no flag" branch. FY2025 additionally has NO reported debt cash
    # flow while the balance-sheet debt rose by 165,782,000: the default-to-0
    # is then not corroborated, so net_debt is None (mismatch), not the
    # 334,451,000 - 18,180,000 the old rule would have used.
    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("ATRO"))}

    assert periods[2025]["net_debt"] is None
    assert DEBT_COMPONENT_MISSING_FLAG in periods[2025]["data_quality_flags"]
    assert DEBT_STOCK_FLOW_MISMATCH_FLAG in periods[2025]["data_quality_flags"]
    assert periods[2024]["net_debt"] == 168_669_000 - 9_285_000
    assert DEBT_COMPONENT_MISSING_FLAG in periods[2024]["data_quality_flags"]

    assert periods[2023]["net_debt"] == (8_996_000 + 159_237_000) - 4_756_000
    assert DEBT_COMPONENT_MISSING_FLAG not in periods[2023]["data_quality_flags"]


def test_debt_component_missing_defaulted_zero_and_reconciled_stays_reliable():
    # CATO FY2020: both Short Term Debt AND Long Term Debt are null, the debt
    # cash flow is a reported 0, and the prior year's balance sheet is null
    # too: dBS 0 vs CF 0 reconciles, so the default-to-0 stands (flagged
    # defaulted_zero only) - even though EBITDA is negative (Entscheidung 3).
    period = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("CATO"))}[2020]
    assert period["net_debt"] == -17_510_000
    assert DEBT_COMPONENT_MISSING_FLAG in period["data_quality_flags"]
    assert NET_DEBT_UNRELIABLE_FLAG not in period["data_quality_flags"]


def test_rock_fy2024_2025_all_debt_fields_missing_is_flagged_not_assumed_debt_free():
    # ROCK FY2024/FY2025: Short Term Debt, Long Term Debt (BS Q4) AND CF
    # "Cash From (Repayment of) Debt" are all null (docs/decisions.md
    # SimFin-Client Entscheidung 10). Before that fix this silently became
    # net_debt = -Cash (-269,480,000 / -115,724,000), i.e. an unverified "no
    # debt at all".
    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("ROCK"))}
    for fiscal_year in (2024, 2025):
        period = periods[fiscal_year]
        assert period["net_debt"] is None
        assert DEBT_DATA_COMPLETELY_MISSING_FLAG in period["data_quality_flags"]
        # this flag is the ONLY reason here - no stock-flow verdict is attempted
        assert NET_DEBT_UNRELIABLE_FLAG not in period["data_quality_flags"]
        assert DEBT_STOCK_FLOW_MISMATCH_FLAG not in period["data_quality_flags"]


def test_rock_fy2023_reported_debt_cash_flow_is_not_completely_missing_and_reconciles():
    # ROCK FY2023: BS debt components are null too, but the debt cash flow is
    # reported (-91,000,000) - so the completely-missing flag must NOT fire.
    # The stock-flow check then confirms that null meant "fully repaid":
    # debt fell from 88,762,000 to (null =) 0, cash flow -91,000,000. FY2023's
    # OWN components are both null (current-year flag) AND FY2022's Short Term
    # Debt is also null (prior-year flag, same flag string) - the two must
    # collapse into one entry, not a duplicate.
    period = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("ROCK"))}[2023]
    assert period["net_debt"] == -99_426_000
    assert NET_DEBT_UNRELIABLE_FLAG not in period["data_quality_flags"]
    assert DEBT_DATA_COMPLETELY_MISSING_FLAG not in period["data_quality_flags"]
    assert period["data_quality_flags"].count(DEBT_COMPONENT_MISSING_FLAG) == 1


def test_reported_zero_debt_cash_flow_is_not_completely_missing():
    # "Reported 0" vs "absent" (SimFin-Client Entscheidung 10). USNA FY2022:
    # both BS debt components null, but the debt cash flow is an explicit 0 - a
    # reported value, so missing components default to 0 (net_debt = -Cash,
    # flagged defaulted_zero only) and dBS 0 vs CF 0 reconciles. USNA FY2021
    # has all three null - flagged and net_debt None.
    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("USNA"), max_years=5)}

    reported_zero = periods[2022]
    assert reported_zero["net_debt"] == -288_420_000
    assert reported_zero["data_quality_flags"] == [DEBT_COMPONENT_MISSING_FLAG]

    absent = periods[2021]
    assert absent["net_debt"] is None
    assert DEBT_DATA_COMPLETELY_MISSING_FLAG in absent["data_quality_flags"]


def test_completely_missing_debt_data_stands_alone_even_with_non_positive_ebitda():
    # CULP FY2023: all three debt fields null AND EBITDA <= 0 (-19,799,000).
    # EBITDA no longer couples to net_debt, so the completely-missing flag is
    # the only verdict - no net_debt_unreliable alongside it.
    period = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("CULP"))}[2023]
    assert period["ebitda"] <= 0
    assert period["net_debt"] is None
    assert DEBT_DATA_COMPLETELY_MISSING_FLAG in period["data_quality_flags"]
    assert NET_DEBT_UNRELIABLE_FLAG not in period["data_quality_flags"]


def test_cato_completely_missing_only_where_the_debt_cash_flow_is_also_null():
    # CATO FY2019: all three null. FY2020: both BS components null but the
    # debt cash flow is an explicit 0 - not "completely missing".
    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("CATO"))}
    assert DEBT_DATA_COMPLETELY_MISSING_FLAG in periods[2019]["data_quality_flags"]
    assert DEBT_DATA_COMPLETELY_MISSING_FLAG not in periods[2020]["data_quality_flags"]


def test_rock_completely_missing_debt_data_makes_net_debt_ebitda_not_evaluable_end_to_end():
    # ROCK's Stichtag year (FY2025) is completely-missing: Net Debt/EBITDA
    # was a valid-looking -0.76x before that fix, now it must be invalid with
    # its own reason, and the company overall not_evaluable. ROIC's first
    # invalid year (FY2024, FY2023 now reconciles) carries the same reason.
    periods = build_company_record("ROCK", _load_raw("ROCK"))["periods"]

    net_debt_ebitda = net_debt_ebitda_latest(periods)
    assert net_debt_ebitda.value is None
    assert net_debt_ebitda.invalid_reason == "debt_data_completely_missing"

    roic = roic_3yr_average(periods)
    assert roic.invalid_reason == "debt_data_completely_missing"

    gate_result = evaluate_stage1_gates(
        roic=roic,
        net_debt_ebitda=net_debt_ebitda,
        cash_conversion=cash_conversion_3yr_average(periods),
        net_income_margin=net_income_margin_3yr_average(periods),
    )
    assert gate_result.status == NOT_EVALUABLE
    assert "debt_data_completely_missing" in gate_result.invalid_reasons


def test_genc_completely_missing_debt_data_poisons_the_whole_3_year_roic_too():
    # GENC: all three anchored years (2023-2025) have all three debt fields
    # null - ROIC and Net Debt/EBITDA both report the own reason.
    periods = build_company_record("GENC", _load_raw("GENC"))["periods"]
    assert roic_3yr_average(periods).invalid_reason == "debt_data_completely_missing"
    assert net_debt_ebitda_latest(periods).invalid_reason == "debt_data_completely_missing"


def test_culp_older_completely_missing_year_poisons_roic_but_not_net_debt_ebitda():
    periods = build_company_record("CULP", _load_raw("CULP"))["periods"]
    # ROIC: FY2023 is completely missing - the whole 3-year value is flagged.
    assert roic_3yr_average(periods).invalid_reason == "debt_data_completely_missing"
    # Net Debt/EBITDA reads only the Stichtag year (FY2025): net_debt is fine
    # there (dBS +12,714,000 vs CF +12,741,000), so the only problem is the
    # non-positive EBITDA - reported under its own, specific reason instead
    # of the former net_debt_unreliable.
    assert net_debt_ebitda_latest(periods).invalid_reason == "ebitda_non_positive"


def test_non_positive_ebitda_year_does_not_block_roic_end_to_end():
    # CENT with FY2025 EBIT forced to -500,000,000 (EBITDA < 0): net_debt is
    # unchanged, ROIC is computed (a negative ROIC is a valid value, the
    # ROIC guards are untouched), and only Net Debt/EBITDA reports the
    # specific ebitda_non_positive (docs/decisions.md Entscheidung 3).
    raw = copy.deepcopy(_load_raw("CENT"))
    pl = next(s for s in raw["statements"] if s["statement"] == "PL")
    fp_idx, fy_idx = pl["columns"].index("Fiscal Period"), pl["columns"].index("Fiscal Year")
    ebit_idx = pl["columns"].index("Operating Income (Loss)")
    for row in pl["data"]:
        if row[fp_idx] == "FY" and row[fy_idx] == 2025:
            row[ebit_idx] = -500_000_000

    periods = build_company_record("CENT", raw)["periods"]
    assert periods[-1]["fiscal_year"] == 2025
    assert periods[-1]["ebitda"] < 0
    assert periods[-1]["net_debt"] == 309_215_000

    roic = roic_3yr_average(periods)
    assert roic.invalid_reason is None
    assert roic.value < 0
    assert net_debt_ebitda_latest(periods).invalid_reason == "ebitda_non_positive"


def test_pl_lags_other_statements_flag_on_every_returned_period_for_sgc():
    # SGC: PL/EBIT ends at FY2023, BS Q4 and CF FY both reach FY2025
    # (Entscheidung 11). Every returned period carries the flag, so it
    # survives whichever period a consumer looks at.
    periods = map_simfin_to_periods(_load_raw("SGC"))
    assert [p["fiscal_year"] for p in periods] == [2021, 2022, 2023]
    for period in periods:
        assert PL_LAGS_OTHER_STATEMENTS_FLAG in period["data_quality_flags"]


def test_pl_lags_other_statements_flag_when_only_bs_reaches_further_for_cato():
    # CATO: PL and CF stop at FY2020, only BS Q4 reaches FY2021 - "BS or CF"
    # is enough to trigger the flag.
    periods = map_simfin_to_periods(_load_raw("CATO"))
    assert [p["fiscal_year"] for p in periods] == [2019, 2020]
    for period in periods:
        assert PL_LAGS_OTHER_STATEMENTS_FLAG in period["data_quality_flags"]


@pytest.mark.parametrize("ticker", ["ATRO", "CENT", "ROCK", "UFPT", "LAKE"])
def test_pl_lags_other_statements_flag_absent_when_pl_is_current(ticker):
    for period in map_simfin_to_periods(_load_raw(ticker)):
        assert PL_LAGS_OTHER_STATEMENTS_FLAG not in period["data_quality_flags"]


def test_pl_lags_flag_is_relative_to_the_newest_pl_year_not_the_truncated_window():
    # max_years=1 anchors only on SGC's newest PL year (2023) - the check
    # must compare against that, not against some older year of the window.
    periods = map_simfin_to_periods(_load_raw("SGC"), max_years=1)
    assert [p["fiscal_year"] for p in periods] == [2023]
    assert PL_LAGS_OTHER_STATEMENTS_FLAG in periods[0]["data_quality_flags"]


@pytest.mark.parametrize("missing_statement", ["BS", "CF"])
def test_missing_bs_or_cf_statement_drops_every_year_instead_of_crashing(missing_statement):
    # docs/decisions.md Entscheidung 12: a whole statement absent is a data
    # gap like a missing value, not a structural error - every candidate year
    # lacks its required fields and is dropped (0 periods => not evaluable
    # downstream), never a silent pass and never a crash.
    raw = _load_raw("ATRO")
    raw["statements"] = [s for s in raw["statements"] if s["statement"] != missing_statement]
    assert map_simfin_to_periods(raw) == []


def test_missing_expected_column_still_fails_loudly():
    # Entscheidung 12: only a missing VALUE (null) in a guaranteed column
    # drops a year. If SimFin's column structure itself changes (an expected
    # column absent from `columns`), the structural failure stays loud - as
    # a SimFinMappingError naming the column, so a batch loop that catches
    # per-company mapping errors keeps going (code review of PR #30).
    raw = copy.deepcopy(_load_raw("ATRO"))
    cf = next(s for s in raw["statements"] if s["statement"] == "CF")
    cf["columns"][cf["columns"].index("Depreciation & Amortization")] = "Renamed By SimFin"
    with pytest.raises(SimFinMappingError, match="Depreciation & Amortization"):
        map_simfin_to_periods(raw)


def test_duplicate_fiscal_year_rows_raise_instead_of_silently_overwriting():
    # A restated/duplicated PL FY row appended after the real one used to
    # replace it silently (later row wins) - here Revenue=1 would have
    # become ATRO's FY2025 revenue with no error.
    raw = copy.deepcopy(_load_raw("ATRO"))
    pl = next(s for s in raw["statements"] if s["statement"] == "PL")
    fp_idx, fy_idx = pl["columns"].index("Fiscal Period"), pl["columns"].index("Fiscal Year")
    real_row = next(r for r in pl["data"] if r[fp_idx] == "FY" and r[fy_idx] == 2025)
    duplicate = list(real_row)
    duplicate[pl["columns"].index("Revenue")] = 1
    pl["data"].append(duplicate)
    with pytest.raises(SimFinMappingError, match="duplicate"):
        map_simfin_to_periods(raw)


def test_year_dropped_flag_on_every_returned_period_when_a_year_had_a_missing_field():
    # MATW FY2025 is dropped (missing total_assets/cash) - both survivors
    # carry the flag, so insufficient_years is traceable to a data gap.
    periods = map_simfin_to_periods(_load_raw("MATW"))
    assert [p["fiscal_year"] for p in periods] == [2023, 2024]
    for period in periods:
        assert YEAR_DROPPED_MISSING_FIELD_FLAG in period["data_quality_flags"]


def test_year_dropped_flag_absent_for_a_genuinely_short_history():
    # CATO has only 2 EBIT-having PL years at all - nothing was dropped, the
    # history is simply short, so this must NOT look like a data gap.
    periods = map_simfin_to_periods(_load_raw("CATO"))
    assert [p["fiscal_year"] for p in periods] == [2019, 2020]
    for period in periods:
        assert YEAR_DROPPED_MISSING_FIELD_FLAG not in period["data_quality_flags"]


@pytest.mark.parametrize("ticker", ["ATRO", "CENT", "SGC"])
def test_year_dropped_flag_absent_when_nothing_was_dropped(ticker):
    for period in map_simfin_to_periods(_load_raw(ticker)):
        assert YEAR_DROPPED_MISSING_FIELD_FLAG not in period["data_quality_flags"]


def test_build_company_record_rejects_a_raw_record_in_another_currency():
    # Checking only the `currency` argument would label EUR figures "USD".
    raw = copy.deepcopy(_load_raw("ATRO"))
    raw["currency"] = "EUR"
    with pytest.raises(SimFinMappingError, match="EUR"):
        build_company_record("ATRO", raw)


def test_build_company_record_rejects_a_mispaired_raw_record():
    # fetch_statements_batch returns a bare list - ATRO's data must not be
    # filed under CENT.
    with pytest.raises(SimFinMappingError, match="ATRO"):
        build_company_record("CENT", _load_raw("ATRO"))


def test_build_company_record_ticker_match_is_case_insensitive():
    assert build_company_record("atro", _load_raw("ATRO"))["company_id"] == "atro"


def test_resolve_simfin_id_skips_entries_with_a_null_ticker():
    company_list = [{"id": 1, "ticker": None}, {"id": 2}, {"id": 3, "ticker": "atro"}]
    assert resolve_simfin_id("ATRO", "key", company_list=company_list) == 3


def test_resolve_simfin_id_unknown_ticker_raises():
    with pytest.raises(SimFinMappingError):
        resolve_simfin_id("NOPE", "key", company_list=[{"id": 1, "ticker": "ATRO"}])


def test_resolve_simfin_id_matched_entry_without_an_id_raises_mapping_error():
    # A companies/list entry that matches by ticker but has no usable id
    # (missing key, or present-and-null) is malformed/schema-drifted, not a
    # lookup miss - it must raise SimFinMappingError like any other mapping
    # failure, not a bare KeyError a per-ticker caller wouldn't catch
    # (code review of PR #32).
    with pytest.raises(SimFinMappingError, match="ATRO"):
        resolve_simfin_id("ATRO", "key", company_list=[{"ticker": "ATRO"}])
    with pytest.raises(SimFinMappingError, match="ATRO"):
        resolve_simfin_id("ATRO", "key", company_list=[{"id": None, "ticker": "ATRO"}])


def test_network_calls_pass_a_request_timeout(monkeypatch):
    # Without a timeout a stalled connection blocks a cron batch forever.
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(timeout)
        return FakeUrlopenResponse(b'[{"id": 7, "ticker": "ATRO"}]')

    monkeypatch.setattr(simfin_client.urllib.request, "urlopen", fake_urlopen)
    assert resolve_simfin_id("ATRO", "key") == 7
    assert fetch_statements_batch([7], "key") == [{"id": 7, "ticker": "ATRO"}]
    assert seen == [REQUEST_TIMEOUT_SECONDS, REQUEST_TIMEOUT_SECONDS]


def test_fetch_companies_list_returns_the_decoded_json(monkeypatch):
    # Issue #9: split out of resolve_simfin_id() so run_stage1.py can fetch
    # this once and reuse it across every ticker via resolve_simfin_id(...,
    # company_list=...) instead of resolve_simfin_id() re-fetching per call.
    def fake_urlopen(req, timeout=None):
        assert req.full_url == simfin_client.SIMFIN_COMPANIES_LIST_URL
        assert timeout == REQUEST_TIMEOUT_SECONDS
        return FakeUrlopenResponse(b'[{"id": 7, "ticker": "ATRO"}, {"id": 8, "ticker": "CATO"}]')

    monkeypatch.setattr(simfin_client.urllib.request, "urlopen", fake_urlopen)
    company_list = fetch_companies_list("key")
    assert company_list == [{"id": 7, "ticker": "ATRO"}, {"id": 8, "ticker": "CATO"}]
    # resolve_simfin_id(..., company_list=...) then does no network call at all
    assert resolve_simfin_id("CATO", "key", company_list=company_list) == 8


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

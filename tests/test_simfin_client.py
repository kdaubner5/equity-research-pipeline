import copy
import io
import json
from pathlib import Path

import pytest

from data_layer import simfin_client
from data_layer.simfin_client import (
    DEBT_COMPONENT_MISSING_FLAG,
    DEBT_DATA_COMPLETELY_MISSING_FLAG,
    DEBT_STATEMENT_MISMATCH_FLAG,
    NET_DEBT_UNRELIABLE_FLAG,
    PL_LAGS_OTHER_STATEMENTS_FLAG,
    REQUEST_TIMEOUT_SECONDS,
    YEAR_DROPPED_MISSING_FIELD_FLAG,
    SimFinMappingError,
    build_company_record,
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
        ("USNA", 2025, None),  # ratio 9,000,000/83,424,000 = 10.79% >= 10%
        ("CATO", 2020, None),  # EBITDA = -79,249,000 + 14,681,000 = -64,568,000 <= 0
        ("ROCK", 2023, None),  # ratio 91,000,000/142,476,000 = 63.87% - well above 10%
        ("UFPT", 2023, None),  # ratio 23,063,000/72,743,000 = 31.70%
        ("UFPT", 2024, None),  # ratio 157,302,000/99,190,000 = 158.58%
        ("UFPT", 2025, None),  # ratio 53,983,000/112,121,000 = 48.15%
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
        ("CENT", 2023, 699_473_000),  # ratio 338,000/298,346,000 = 0.11%
        ("CENT", 2024, 436_498_000),  # ratio 370,000/276,194,000 = 0.13%
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


def test_lake_every_year_inside_the_3_year_window_is_net_debt_unreliable():
    # LAKE's window is FY2023-2025 (its own 3 most recent EBIT-having
    # years); none of the three is under the 10% threshold (22.1% / 333.3% /
    # EBITDA <= 0). LAKE's <10% ratios exist only in FY2019-2022, which the
    # adapter never evaluates - outside the window, not an exception to it.
    periods = map_simfin_to_periods(_load_raw("LAKE"))
    assert [p["fiscal_year"] for p in periods] == [2023, 2024, 2025]
    for period in periods:
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


def test_rock_fy2024_2025_all_debt_fields_missing_is_flagged_not_assumed_debt_free():
    # ROCK FY2024/FY2025: Short Term Debt, Long Term Debt (BS Q4) AND CF
    # "Cash From (Repayment of) Debt" are all null (docs/decisions.md
    # Entscheidung 10). Before the fix this silently became net_debt = -Cash
    # (-269,480,000 / -115,724,000), i.e. an unverified "no debt at all".
    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("ROCK"))}
    for fiscal_year in (2024, 2025):
        period = periods[fiscal_year]
        assert period["net_debt"] is None
        assert DEBT_DATA_COMPLETELY_MISSING_FLAG in period["data_quality_flags"]
        # EBITDA > 0 and no debt cash flow to form a ratio from: this flag is
        # the ONLY reason net_debt is None here, not the ratio/EBITDA guard.
        assert NET_DEBT_UNRELIABLE_FLAG not in period["data_quality_flags"]


def test_rock_fy2023_reported_debt_cash_flow_is_not_completely_missing():
    # ROCK FY2023: BS debt components are null too, but the debt cash flow is
    # reported (-91,000,000) - so the flag must NOT fire; net_debt is None
    # via the ordinary ratio guard (63.87% >= 10%) instead.
    period = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("ROCK"))}[2023]
    assert period["net_debt"] is None
    assert NET_DEBT_UNRELIABLE_FLAG in period["data_quality_flags"]
    assert DEBT_DATA_COMPLETELY_MISSING_FLAG not in period["data_quality_flags"]


def test_reported_zero_debt_cash_flow_is_not_completely_missing():
    # "Reported 0" vs "absent" (Entscheidung 10). USNA FY2022: both BS debt
    # components null, but the debt cash flow is an explicit 0 - a reported
    # value, so the pre-existing behaviour holds (missing components default
    # to 0, net_debt = -Cash, flagged defaulted_zero only). USNA FY2021 has
    # all three null - flagged and net_debt None.
    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("USNA"), max_years=5)}

    reported_zero = periods[2022]
    assert reported_zero["net_debt"] == -288_420_000
    assert reported_zero["data_quality_flags"] == [DEBT_COMPONENT_MISSING_FLAG]

    absent = periods[2021]
    assert absent["net_debt"] is None
    assert DEBT_DATA_COMPLETELY_MISSING_FLAG in absent["data_quality_flags"]


def test_completely_missing_debt_data_and_non_positive_ebitda_set_both_flags():
    # CULP FY2023: all three debt fields null AND EBITDA <= 0 (-19,799,000).
    # Both conditions hold, so both flags are set (transparency); which one
    # the metrics layer reports is covered further down.
    period = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("CULP"))}[2023]
    assert period["net_debt"] is None
    assert DEBT_DATA_COMPLETELY_MISSING_FLAG in period["data_quality_flags"]
    assert NET_DEBT_UNRELIABLE_FLAG in period["data_quality_flags"]


def test_cato_completely_missing_only_where_the_debt_cash_flow_is_also_null():
    # CATO FY2019: all three null. FY2020: both BS components null but the
    # debt cash flow is an explicit 0 - not "completely missing".
    periods = {p["fiscal_year"]: p for p in map_simfin_to_periods(_load_raw("CATO"))}
    assert DEBT_DATA_COMPLETELY_MISSING_FLAG in periods[2019]["data_quality_flags"]
    assert DEBT_DATA_COMPLETELY_MISSING_FLAG not in periods[2020]["data_quality_flags"]


def test_rock_completely_missing_debt_data_makes_net_debt_ebitda_not_evaluable_end_to_end():
    # ROCK's Stichtag year (FY2025) is completely-missing: Net Debt/EBITDA
    # was a valid-looking -0.76x before the fix, now it must be invalid with
    # its own reason, and the company overall not_evaluable.
    periods = build_company_record("ROCK", _load_raw("ROCK"))["periods"]

    net_debt_ebitda = net_debt_ebitda_latest(periods)
    assert net_debt_ebitda.value is None
    assert net_debt_ebitda.invalid_reason == "debt_data_completely_missing"

    # ROIC reports the first invalid year in fiscal-year order, like every
    # ROIC guard: FY2023 (ratio guard) precedes the completely-missing FY2024/25,
    # so ROIC says net_debt_unreliable while Net Debt/EBITDA (Stichtag FY2025
    # only) says debt_data_completely_missing (docs/decisions.md Entscheidung 10).
    roic = roic_3yr_average(periods)
    assert roic.invalid_reason == "net_debt_unreliable"

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


def test_culp_completely_missing_reason_wins_in_roic_but_does_not_leak_into_net_debt_ebitda():
    periods = build_company_record("CULP", _load_raw("CULP"))["periods"]
    # ROIC: FY2023 carries both flags (all-null AND EBITDA <= 0) - the
    # completely-missing reason takes precedence over net_debt_unreliable.
    assert roic_3yr_average(periods).invalid_reason == "debt_data_completely_missing"
    # Net Debt/EBITDA reads only the Stichtag year (FY2025), which is merely
    # unreliable (ratio/EBITDA), not completely missing - the older
    # FY2023 must not change that.
    assert net_debt_ebitda_latest(periods).invalid_reason == "net_debt_unreliable"


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


class _FakeResponse(io.BytesIO):
    pass


def test_network_calls_pass_a_request_timeout(monkeypatch):
    # Without a timeout a stalled connection blocks a cron batch forever.
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(timeout)
        return _FakeResponse(b'[{"id": 7, "ticker": "ATRO"}]')

    monkeypatch.setattr(simfin_client.urllib.request, "urlopen", fake_urlopen)
    assert resolve_simfin_id("ATRO", "key") == 7
    assert fetch_statements_batch([7], "key") == [{"id": 7, "ticker": "ATRO"}]
    assert seen == [REQUEST_TIMEOUT_SECONDS, REQUEST_TIMEOUT_SECONDS]


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

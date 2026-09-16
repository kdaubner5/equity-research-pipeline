import json
from pathlib import Path

import pytest

from data_layer.edgar_client import (
    CASH_INCLUDES_RESTRICTED_FLAG,
    DA_INCOMPLETE_FLAG,
    DEBT_COMPONENT_MISSING_FLAG,
    NET_INCOME_INCLUDES_NCI_FLAG,
    REVENUE_INCLUDING_ASSESSED_TAX_FLAG,
    EdgarMappingError,
    build_company_record,
    map_companyfacts_to_periods,
)
from metrics_layer.roic import roic_for_period

EDGAR_RAW_DIR = Path(__file__).parent / "fixtures" / "edgar_raw"


def _load_raw(ticker):
    with open(EDGAR_RAW_DIR / f"{ticker}.json", encoding="utf-8") as f:
        return json.load(f)


def _usd_fact(val, end, start=None, fy=None, fp="FY", form="10-K", filed="2025-01-01"):
    point = {"end": end, "val": val, "fy": fy, "fp": fp, "form": form, "filed": filed}
    if start is not None:
        point["start"] = start
    return point


def _minimal_companyfacts(tags):
    """A minimal synthetic companyfacts dict with only the given us-gaap tags."""
    return {"entityName": "Synthetic Test Co", "facts": {"us-gaap": tags}}


@pytest.mark.parametrize("ticker", ["MOV", "NPK", "LAKE", "FOSL"])
def test_real_companies_produce_complete_plausible_records(ticker):
    record = build_company_record(ticker, _load_raw(ticker))

    assert record["company_id"] == ticker
    assert record["currency"] == "USD"
    assert record["sector"] is None
    assert record["name"]

    periods = record["periods"]
    assert 1 <= len(periods) <= 3
    fiscal_years = [p["fiscal_year"] for p in periods]
    assert fiscal_years == sorted(fiscal_years)
    assert len(fiscal_years) == len(set(fiscal_years))

    for period in periods:
        for field in ("revenue", "ebit", "ebitda", "equity", "net_debt", "net_income", "cfo"):
            assert isinstance(period[field], (int, float))
        assert period["tax_rate"] is None or isinstance(period["tax_rate"], float)
        assert isinstance(period["data_quality_flags"], list)


def test_fiscal_year_uses_companys_own_fy_label_not_end_date_calendar_year():
    # FOSL's FY2025 ends 2026-01-03 (52/53-week fiscal calendar) - naively
    # taking the calendar year of the end date would mislabel it 2026.
    periods = map_companyfacts_to_periods(_load_raw("FOSL"))
    fiscal_years = [p["fiscal_year"] for p in periods]
    assert fiscal_years == [2023, 2024, 2025]


def test_tax_rate_undefined_when_pretax_income_non_positive():
    # LAKE has 1 healthy year and 2 years with negative pretax income
    # (mirrors the ROIC guard's fixture shape, but from real EDGAR data).
    periods = {p["fiscal_year"]: p for p in map_companyfacts_to_periods(_load_raw("LAKE"))}
    assert periods[2024]["tax_rate"] is not None
    assert periods[2025]["tax_rate"] is None
    assert periods[2026]["tax_rate"] is None

    # confirm this actually flows into the Branch 1 guard, not just a
    # coincidentally-None field the metrics layer never sees
    result = roic_for_period(
        ebit=periods[2025]["ebit"],
        tax_rate=periods[2025]["tax_rate"],
        equity=periods[2025]["equity"],
        net_debt=periods[2025]["net_debt"],
    )
    assert result.value is None
    assert result.invalid_reason == "undefined_tax_rate"


def test_da_falls_back_to_depreciation_and_flags_it_as_incomplete():
    # NPK reports DepreciationDepletionAndAmortization through FY2023, then
    # switches to the plain (possibly incomplete) Depreciation tag.
    periods = {p["fiscal_year"]: p for p in map_companyfacts_to_periods(_load_raw("NPK"))}

    # NPK is also missing debt tags in every year (see
    # test_missing_debt_tags_default_to_zero_not_invalid) - DA_INCOMPLETE_FLAG
    # is the only thing this test cares about, not the full flag list.
    assert DA_INCOMPLETE_FLAG not in periods[2023]["data_quality_flags"]
    assert periods[2023]["ebitda"] == pytest.approx(periods[2023]["ebit"] + 6_007_000)

    for fy in (2024, 2025):
        assert DA_INCOMPLETE_FLAG in periods[fy]["data_quality_flags"]


def test_cash_falls_back_to_restricted_cash_tag_and_flags_it():
    # LAKE stops reporting CashAndCashEquivalentsAtCarryingValue after
    # FY2024 and switches to the combined restricted-cash tag.
    periods = {p["fiscal_year"]: p for p in map_companyfacts_to_periods(_load_raw("LAKE"))}

    # LAKE is also missing debt tags in every year (see
    # test_missing_debt_tags_default_to_zero_not_invalid) - CASH_INCLUDES_RESTRICTED_FLAG
    # is the only thing this test cares about, not the full flag list.
    assert CASH_INCLUDES_RESTRICTED_FLAG not in periods[2024]["data_quality_flags"]
    for fy in (2025, 2026):
        assert CASH_INCLUDES_RESTRICTED_FLAG in periods[fy]["data_quality_flags"]


def test_net_income_falls_back_to_profit_loss_and_flags_nci():
    # FOSL's NetIncomeLoss tag stops at FY2011; every pilot-window year
    # comes from ProfitLoss instead (includes noncontrolling interest).
    periods = map_companyfacts_to_periods(_load_raw("FOSL"))
    assert len(periods) == 3
    for period in periods:
        assert NET_INCOME_INCLUDES_NCI_FLAG in period["data_quality_flags"]
    by_year = {p["fiscal_year"]: p for p in periods}
    assert by_year[2025]["net_income"] == -77_837_000


@pytest.mark.parametrize("ticker", ["MOV", "NPK"])
def test_missing_debt_tags_default_to_zero_not_invalid(ticker):
    # Neither MOV nor NPK reports LongTermDebtNoncurrent/Current or
    # ShortTermBorrowings in recent years - a real net-cash position, not
    # missing data, so net_debt must equal exactly -cash (total debt = 0),
    # and the gap must be visible via the flag, not just inferred from the sign.
    record = build_company_record(ticker, _load_raw(ticker))
    for period in record["periods"]:
        assert period["net_debt"] < 0
        assert DEBT_COMPONENT_MISSING_FLAG in period["data_quality_flags"]


def test_debt_components_all_present_sum_correctly_without_flag():
    # None of the 4 pilot tickers report all three debt-component tags (see
    # test_missing_debt_tags_default_to_zero_not_invalid above) - a synthetic
    # minimal companyfacts dict exercises the "fully reported, no flag"
    # branch directly, so a sign/field-order regression in the sum is still
    # caught even without a pilot ticker for it.
    tags = {
        "OperatingIncomeLoss": {"units": {"USD": [_usd_fact(100, "2024-12-31", "2024-01-01", fy=2024)]}},
        "DepreciationDepletionAndAmortization": {
            "units": {"USD": [_usd_fact(10, "2024-12-31", "2024-01-01", fy=2024)]}
        },
        "IncomeTaxExpenseBenefit": {"units": {"USD": [_usd_fact(20, "2024-12-31", "2024-01-01", fy=2024)]}},
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": {
            "units": {"USD": [_usd_fact(80, "2024-12-31", "2024-01-01", fy=2024)]}
        },
        "StockholdersEquity": {"units": {"USD": [_usd_fact(500, "2024-12-31", fy=2024)]}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [_usd_fact(50, "2024-12-31", fy=2024)]}},
        "NetCashProvidedByUsedInOperatingActivities": {
            "units": {"USD": [_usd_fact(90, "2024-12-31", "2024-01-01", fy=2024)]}
        },
        "NetIncomeLoss": {"units": {"USD": [_usd_fact(60, "2024-12-31", "2024-01-01", fy=2024)]}},
        "Revenues": {"units": {"USD": [_usd_fact(1000, "2024-12-31", "2024-01-01", fy=2024)]}},
        "LongTermDebtNoncurrent": {"units": {"USD": [_usd_fact(300, "2024-12-31", fy=2024)]}},
        "LongTermDebtCurrent": {"units": {"USD": [_usd_fact(40, "2024-12-31", fy=2024)]}},
        "ShortTermBorrowings": {"units": {"USD": [_usd_fact(15, "2024-12-31", fy=2024)]}},
    }
    periods = map_companyfacts_to_periods(_minimal_companyfacts(tags), max_years=1)
    assert len(periods) == 1
    assert periods[0]["net_debt"] == (300 + 40 + 15) - 50
    assert DEBT_COMPONENT_MISSING_FLAG not in periods[0]["data_quality_flags"]


def test_revenue_prefers_excluding_assessed_tax_tag_when_present():
    # MOV and FOSL both have the top-priority tag for every period in the
    # pilot window - the flagged "IncludingAssessedTax" fallback should
    # never fire for them.
    for ticker in ("MOV", "FOSL"):
        periods = map_companyfacts_to_periods(_load_raw(ticker))
        for period in periods:
            assert REVENUE_INCLUDING_ASSESSED_TAX_FLAG not in period["data_quality_flags"]


def test_revenue_falls_back_to_plain_revenues_tag_without_flag():
    # NPK never adopted a RevenueFromContractWithCustomer... tag at all -
    # falls to the 2nd-priority "Revenues" tag, which is not itself flagged.
    periods = map_companyfacts_to_periods(_load_raw("NPK"))
    for period in periods:
        assert REVENUE_INCLUDING_ASSESSED_TAX_FLAG not in period["data_quality_flags"]


def test_revenue_including_assessed_tax_fallback_flags_when_it_is_all_thats_available():
    # None of the 4 pilot companies ever exercises this branch for real
    # (see docs/decisions.md / pilot report) - a synthetic minimal
    # companyfacts dict exercises it directly so the branch itself is
    # still verified, not just documented as untested.
    tags = {
        "OperatingIncomeLoss": {"units": {"USD": [_usd_fact(100, "2024-12-31", "2024-01-01", fy=2024)]}},
        "DepreciationDepletionAndAmortization": {
            "units": {"USD": [_usd_fact(10, "2024-12-31", "2024-01-01", fy=2024)]}
        },
        "IncomeTaxExpenseBenefit": {"units": {"USD": [_usd_fact(20, "2024-12-31", "2024-01-01", fy=2024)]}},
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": {
            "units": {"USD": [_usd_fact(80, "2024-12-31", "2024-01-01", fy=2024)]}
        },
        "StockholdersEquity": {"units": {"USD": [_usd_fact(500, "2024-12-31", fy=2024)]}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [_usd_fact(50, "2024-12-31", fy=2024)]}},
        "NetCashProvidedByUsedInOperatingActivities": {
            "units": {"USD": [_usd_fact(90, "2024-12-31", "2024-01-01", fy=2024)]}
        },
        "NetIncomeLoss": {"units": {"USD": [_usd_fact(60, "2024-12-31", "2024-01-01", fy=2024)]}},
        "RevenueFromContractWithCustomerIncludingAssessedTax": {
            "units": {"USD": [_usd_fact(1000, "2024-12-31", "2024-01-01", fy=2024)]}
        },
    }
    periods = map_companyfacts_to_periods(_minimal_companyfacts(tags), max_years=1)
    assert len(periods) == 1
    assert periods[0]["revenue"] == 1000
    assert REVENUE_INCLUDING_ASSESSED_TAX_FLAG in periods[0]["data_quality_flags"]


def test_missing_anchor_tag_raises_mapping_error():
    with pytest.raises(EdgarMappingError):
        map_companyfacts_to_periods(_minimal_companyfacts({}))


def test_build_company_record_rejects_non_usd_currency():
    # No non-USD data source or conversion exists (docs/masterplan.md scopes
    # this to US tickers) - every tag lookup reads the "USD" unit
    # unconditionally, so a non-"USD" currency argument must fail loudly
    # rather than silently mislabel USD figures with the wrong currency.
    with pytest.raises(EdgarMappingError):
        build_company_record("MOV", _load_raw("MOV"), currency="EUR")


def test_anchor_tag_duplicate_with_fy_none_raises_mapping_error():
    # SEC's schema does not guarantee every duplicate point carries a `fy`
    # value - no pilot ticker shows this, so a synthetic minimal
    # companyfacts dict exercises it: a fy=None duplicate must raise
    # EdgarMappingError, not a bare TypeError out of min().
    tags = {
        "OperatingIncomeLoss": {
            "units": {
                "USD": [
                    _usd_fact(100, "2024-12-31", "2024-01-01", fy=2024),
                    _usd_fact(100, "2024-12-31", "2024-01-01", fy=None, filed="2025-06-01"),
                ]
            }
        },
    }
    with pytest.raises(EdgarMappingError):
        map_companyfacts_to_periods(_minimal_companyfacts(tags), max_years=1)


def test_missing_non_defaultable_concept_raises_mapping_error():
    # EBIT present, but nothing else - every non-debt concept has no
    # accepted "missing = 0" default (unlike the 3 debt components).
    tags = {
        "OperatingIncomeLoss": {"units": {"USD": [_usd_fact(100, "2024-12-31", "2024-01-01", fy=2024)]}},
    }
    with pytest.raises(EdgarMappingError):
        map_companyfacts_to_periods(_minimal_companyfacts(tags), max_years=1)


def test_quarterly_stub_period_tagged_as_fy_is_excluded():
    # A quarterly context can still carry form="10-K"/fp="FY" in SEC's raw
    # data (docs/decisions.md) - only a ~350-380 day span counts as annual.
    tags = {
        "OperatingIncomeLoss": {
            "units": {
                "USD": [
                    _usd_fact(999, "2024-10-31", "2024-08-01", fy=2024),  # ~91 days: quarterly stub
                    _usd_fact(100, "2024-12-31", "2024-01-01", fy=2024),  # ~365 days: real annual period
                ]
            }
        },
        "DepreciationDepletionAndAmortization": {
            "units": {"USD": [_usd_fact(10, "2024-12-31", "2024-01-01", fy=2024)]}
        },
        "IncomeTaxExpenseBenefit": {"units": {"USD": [_usd_fact(20, "2024-12-31", "2024-01-01", fy=2024)]}},
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": {
            "units": {"USD": [_usd_fact(80, "2024-12-31", "2024-01-01", fy=2024)]}
        },
        "StockholdersEquity": {"units": {"USD": [_usd_fact(500, "2024-12-31", fy=2024)]}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [_usd_fact(50, "2024-12-31", fy=2024)]}},
        "NetCashProvidedByUsedInOperatingActivities": {
            "units": {"USD": [_usd_fact(90, "2024-12-31", "2024-01-01", fy=2024)]}
        },
        "NetIncomeLoss": {"units": {"USD": [_usd_fact(60, "2024-12-31", "2024-01-01", fy=2024)]}},
        "Revenues": {"units": {"USD": [_usd_fact(1000, "2024-12-31", "2024-01-01", fy=2024)]}},
    }
    periods = map_companyfacts_to_periods(_minimal_companyfacts(tags), max_years=1)
    assert len(periods) == 1
    assert periods[0]["ebit"] == 100


def test_fewer_than_max_years_anchor_periods_returns_only_available_years():
    # A short EBIT history (e.g. a recent IPO) has fewer anchor period ends
    # than max_years - no pilot ticker shows this, so a synthetic minimal
    # companyfacts dict exercises it directly: requesting the default 3
    # years back with only 2 EBIT period ends available must return exactly
    # those 2, not crash and not pad/fabricate a 3rd year.
    tags = {
        "OperatingIncomeLoss": {
            "units": {
                "USD": [
                    _usd_fact(100, "2023-12-31", "2023-01-01", fy=2023),
                    _usd_fact(120, "2024-12-31", "2024-01-01", fy=2024),
                ]
            }
        },
        "DepreciationDepletionAndAmortization": {
            "units": {
                "USD": [
                    _usd_fact(10, "2023-12-31", "2023-01-01", fy=2023),
                    _usd_fact(12, "2024-12-31", "2024-01-01", fy=2024),
                ]
            }
        },
        "IncomeTaxExpenseBenefit": {
            "units": {
                "USD": [
                    _usd_fact(20, "2023-12-31", "2023-01-01", fy=2023),
                    _usd_fact(24, "2024-12-31", "2024-01-01", fy=2024),
                ]
            }
        },
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": {
            "units": {
                "USD": [
                    _usd_fact(80, "2023-12-31", "2023-01-01", fy=2023),
                    _usd_fact(96, "2024-12-31", "2024-01-01", fy=2024),
                ]
            }
        },
        "StockholdersEquity": {
            "units": {
                "USD": [
                    _usd_fact(500, "2023-12-31", fy=2023),
                    _usd_fact(560, "2024-12-31", fy=2024),
                ]
            }
        },
        "CashAndCashEquivalentsAtCarryingValue": {
            "units": {
                "USD": [
                    _usd_fact(50, "2023-12-31", fy=2023),
                    _usd_fact(55, "2024-12-31", fy=2024),
                ]
            }
        },
        "NetCashProvidedByUsedInOperatingActivities": {
            "units": {
                "USD": [
                    _usd_fact(90, "2023-12-31", "2023-01-01", fy=2023),
                    _usd_fact(95, "2024-12-31", "2024-01-01", fy=2024),
                ]
            }
        },
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    _usd_fact(60, "2023-12-31", "2023-01-01", fy=2023),
                    _usd_fact(72, "2024-12-31", "2024-01-01", fy=2024),
                ]
            }
        },
        "Revenues": {
            "units": {
                "USD": [
                    _usd_fact(1000, "2023-12-31", "2023-01-01", fy=2023),
                    _usd_fact(1100, "2024-12-31", "2024-01-01", fy=2024),
                ]
            }
        },
    }
    periods = map_companyfacts_to_periods(_minimal_companyfacts(tags))
    assert len(periods) == 2
    assert [p["fiscal_year"] for p in periods] == [2023, 2024]

"""Capex line per fiscal year from raw SimFin statements (docs/decisions.md "Stufe 2:
Peer-Gruppen und relative Bewertung", Abschnitte 1 und 6, Test 5). Artificial raw
records in the SimFin compact format - no fixtures from data/, no network."""

import pytest

from data_layer.simfin_client import SimFinMappingError
from data_layer.simfin_valuation import CAPEX_COLUMN, extract_capex_by_fiscal_year


def _cf(rows, columns=("Fiscal Period", "Fiscal Year", "Cash from Operating Activities", CAPEX_COLUMN)):
    return {"statement": "CF", "columns": list(columns), "data": rows}


def _company(*statements):
    return {"ticker": "TEST", "statements": list(statements)}


def test_capex_per_fiscal_year_from_the_fy_rows():
    company = _company(_cf([["FY", 2023, 100.0, -30.0], ["FY", 2024, 110.0, -35.0]]))
    assert extract_capex_by_fiscal_year(company) == {2023: -30.0, 2024: -35.0}


def test_quarter_rows_are_ignored():
    company = _company(_cf([["Q1", 2024, 20.0, -5.0], ["FY", 2024, 110.0, -35.0], ["Q4", 2024, 30.0, -9.0]]))
    assert extract_capex_by_fiscal_year(company) == {2024: -35.0}


def test_net_inflow_is_returned_as_is_the_sign_rule_lives_in_the_metric():
    company = _company(_cf([["FY", 2024, 110.0, 8277000.0]]))
    assert extract_capex_by_fiscal_year(company) == {2024: 8277000.0}


def test_null_capex_stays_none_it_is_not_filled_with_zero():
    company = _company(_cf([["FY", 2024, 110.0, None]]))
    assert extract_capex_by_fiscal_year(company) == {2024: None}


def test_zero_capex_stays_zero():
    company = _company(_cf([["FY", 2024, 110.0, 0]]))
    assert extract_capex_by_fiscal_year(company) == {2024: 0}


def test_missing_cash_flow_statement_gives_an_empty_map():
    company = _company({"statement": "PL", "columns": ["Fiscal Period", "Fiscal Year"], "data": []})
    assert extract_capex_by_fiscal_year(company) == {}


def test_no_statements_at_all_gives_an_empty_map():
    assert extract_capex_by_fiscal_year({"ticker": "TEST"}) == {}


def test_missing_capex_column_raises_instead_of_guessing():
    company = _company(_cf([["FY", 2024, 110.0]], columns=("Fiscal Period", "Fiscal Year", "Cash from Operating Activities")))
    with pytest.raises(SimFinMappingError, match="Change in Fixed Assets"):
        extract_capex_by_fiscal_year(company)


def test_duplicate_fiscal_year_rows_raise_like_the_mapper():
    company = _company(_cf([["FY", 2024, 110.0, -35.0], ["FY", 2024, 111.0, -36.0]]))
    with pytest.raises(SimFinMappingError, match="duplicate"):
        extract_capex_by_fiscal_year(company)


def test_missing_fiscal_year_column_raises():
    company = _company(_cf([["FY", 110.0, -35.0]], columns=("Fiscal Period", "Cash from Operating Activities", CAPEX_COLUMN)))
    with pytest.raises(SimFinMappingError, match="Fiscal Year"):
        extract_capex_by_fiscal_year(company)

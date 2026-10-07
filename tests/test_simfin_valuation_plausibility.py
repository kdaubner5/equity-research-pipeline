"""Dividends paid and Other Investing per fiscal year from raw SimFin statements
(docs/decisions.md "Stufe 2 - Datenplausibilitaet: Aktienzahl und Investitionen", Abschnitt 5, Test 4)."""

import pytest

from data_layer.simfin_client import SimFinMappingError
from data_layer.simfin_valuation import (
    DIVIDENDS_PAID_COLUMN,
    OTHER_INVESTING_COLUMN,
    extract_dividends_paid_by_fiscal_year,
    extract_other_investing_by_fiscal_year,
)

EXTRACTORS = [
    (extract_dividends_paid_by_fiscal_year, DIVIDENDS_PAID_COLUMN),
    (extract_other_investing_by_fiscal_year, OTHER_INVESTING_COLUMN),
]


def _company(column, rows):
    return {
        "ticker": "T",
        "statements": [{"statement": "CF", "columns": ["Fiscal Period", "Fiscal Year", "Cash from Operating Activities", column], "data": rows}],
    }


def test_the_column_names_are_the_simfin_names():
    assert DIVIDENDS_PAID_COLUMN == "Dividends Paid"
    assert OTHER_INVESTING_COLUMN == "Other Investing Activities"


@pytest.mark.parametrize("extract,column", EXTRACTORS)
def test_value_per_fiscal_year_from_the_fy_rows_only(extract, column):
    company = _company(column, [["Q1", 2024, 1.0, -9.0], ["FY", 2023, 100.0, -30.0], ["FY", 2024, 110.0, -35.0], ["Q4", 2024, 1.0, -8.0]])
    assert extract(company) == {2023: -30.0, 2024: -35.0}


@pytest.mark.parametrize("extract,column", EXTRACTORS)
def test_null_stays_none_and_zero_stays_zero(extract, column):
    company = _company(column, [["FY", 2023, 100.0, None], ["FY", 2024, 110.0, 0]])
    assert extract(company) == {2023: None, 2024: 0}


@pytest.mark.parametrize("extract,column", EXTRACTORS)
def test_a_positive_value_is_returned_unchanged(extract, column):
    assert extract(_company(column, [["FY", 2024, 1.0, 27.0]])) == {2024: 27.0}


@pytest.mark.parametrize("extract,column", EXTRACTORS)
def test_missing_cash_flow_statement_gives_an_empty_map(extract, column):
    assert extract({"ticker": "T", "statements": [{"statement": "PL", "columns": ["Fiscal Period", "Fiscal Year"], "data": []}]}) == {}
    assert extract({"ticker": "T"}) == {}


@pytest.mark.parametrize("extract,column", EXTRACTORS)
def test_missing_column_raises_naming_it(extract, column):
    company = {"ticker": "T", "statements": [{"statement": "CF", "columns": ["Fiscal Period", "Fiscal Year"], "data": [["FY", 2024]]}]}
    with pytest.raises(SimFinMappingError, match=column.split()[0]):
        extract(company)


@pytest.mark.parametrize("extract,column", EXTRACTORS)
def test_duplicate_fiscal_year_rows_raise_like_the_mapper(extract, column):
    with pytest.raises(SimFinMappingError, match="duplicate"):
        extract(_company(column, [["FY", 2024, 1.0, -1.0], ["FY", 2024, 2.0, -2.0]]))

"""Capex input for the Stage-2 FCF-yield (docs/decisions.md "Stufe 2: Peer-Gruppen und
relative Bewertung", Abschnitt 1).

The Stage-1 mapper (simfin_client.map_simfin_to_periods) does not read the capex line,
and a required-field change there would drop fiscal years and move Stage-1 results.
This separate, additive, pure and network-free function reads it directly from the raw
SimFin cash-flow statement instead.
"""

from data_layer.simfin_client import SimFinMappingError, _column_index, _rows_by_fiscal_year, _statement

# SimFin's net investment line of the cash-flow statement: money paid for property, plant,
# equipment and intangibles, net of disposals. Negative = cash outflow, positive = net
# inflow from asset sales. A data-format name, not a threshold.
CAPEX_COLUMN = "Change in Fixed Assets & Intangibles"


# Cash-flow lines for the Stage-2 plausibility checks (docs/decisions.md "Stufe 2 -
# Datenplausibilitaet: Aktienzahl und Investitionen"). Data-format names, not thresholds.
# Dividends Paid: negative = cash paid out to shareholders. Other Investing Activities: the
# residual investing line (may hold capex that SimFin did not classify, securities, acquisitions;
# negative = outflow, positive = inflow).
DIVIDENDS_PAID_COLUMN = "Dividends Paid"
OTHER_INVESTING_COLUMN = "Other Investing Activities"


def _cash_flow_column_by_fiscal_year(raw_company, column):
    cf = _statement(raw_company, "CF")
    if cf is None:
        return {}
    idx = _column_index(cf["columns"], column)
    rows = _rows_by_fiscal_year(cf, "FY")
    return {year: row[idx] for year, row in rows.items()}


def extract_dividends_paid_by_fiscal_year(raw_company):
    """{fiscal_year: raw value of DIVIDENDS_PAID_COLUMN}; same contract as
    extract_capex_by_fiscal_year (raw value unchanged, None stays None, {} without a CF
    statement, SimFinMappingError for a missing column or duplicate fiscal-year rows)."""
    return _cash_flow_column_by_fiscal_year(raw_company, DIVIDENDS_PAID_COLUMN)


def extract_other_investing_by_fiscal_year(raw_company):
    """{fiscal_year: raw value of OTHER_INVESTING_COLUMN}; same contract as
    extract_dividends_paid_by_fiscal_year."""
    return _cash_flow_column_by_fiscal_year(raw_company, OTHER_INVESTING_COLUMN)


def extract_capex_by_fiscal_year(raw_company):
    """{fiscal_year: raw value of CAPEX_COLUMN} for the FY rows of one company's CF statement.

    The raw value is returned unchanged (sign included): None stays None - a missing value is
    never turned into 0 - and the net-inflow rule lives in metrics_layer.valuation. A company
    without any CF statement gives {} (the metric then reports capex_missing for every year).
    Raises SimFinMappingError if the statement lacks the capex or a key column, or has two
    rows for the same fiscal year (same refusal to guess as the Stage-1 mapper).
    """
    cf = _statement(raw_company, "CF")
    if cf is None:
        return {}
    capex_idx = _column_index(cf["columns"], CAPEX_COLUMN)
    rows = _rows_by_fiscal_year(cf, "FY")
    return {year: row[capex_idx] for year, row in rows.items()}

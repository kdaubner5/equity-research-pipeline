"""Full-chain run over the 23 SimFin pilot raw fixtures: adapter -> metrics -> gates -> store.

A characterization snapshot, not a correctness claim: it pins what the
current rules produce for the real pilot data, so a rule change shows up as a
visible, reviewable diff of this table instead of a silent shift in who
survives (docs/decisions.md, "net_debt-Zuverlaessigkeit - Stock-Flow-Abgleich",
Entscheidung 6). When a rule is changed on purpose, regenerate the snapshot
(run this file as a script) and review the difference - do not "repair" the
expected values to make the test green.

    python tests/test_simfin_pilot_gates.py      # prints the table and the distribution

The 23 tickers are a mappability sample from the Issue-#8 timebox (4 EDGAR
pilots + 19 of the 20 timebox tickers), not a representative cross-section of
the universe - the distribution says how the rules behave on this data, not
what share of the universe would survive.
"""

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

if __name__ == "__main__":  # allow running as a plain script, outside pytest's pythonpath setting
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from data_layer.simfin_client import build_company_record
from gates_layer.stage1_gates import (
    FAILED,
    NOT_EVALUABLE,
    PASSED,
    PASSED_WITH_FLAGS,
    collect_data_quality_flags,
    evaluate_stage1_gates,
    rank_companies,
)
from metrics_layer.cash_conversion import cash_conversion_3yr_average, net_income_margin_3yr_average
from metrics_layer.net_debt_ebitda import net_debt_ebitda_latest
from metrics_layer.roic import roic_3yr_average
from store_layer.db import save_run_results

SIMFIN_RAW_DIR = Path(__file__).parent / "fixtures" / "simfin_raw"

# ticker -> (status, metric flags, invalid_reasons) - see the module docstring.
EXPECTED_OUTCOMES = {
    "ATRO": ("not_evaluable", (), ("net_debt_unreliable", "net_debt_unreliable", "ni_non_positive")),
    "CATO": ("not_evaluable", (), ("insufficient_years", "ebitda_non_positive", "insufficient_years", "insufficient_years")),
    "CENT": ("failed", (), ()),
    "CULP": ("not_evaluable", (), ("debt_data_completely_missing", "ebitda_non_positive", "ni_non_positive")),
    "CVGI": ("not_evaluable", (), ("ni_non_positive",)),
    "DXPE": ("failed", (), ()),
    "FLXS": ("passed_with_flags", ("roic",), ()),
    "FOSL": ("not_evaluable", (), ("net_debt_unreliable", "net_debt_unreliable", "ni_non_positive")),
    "GENC": ("not_evaluable", (), ("debt_data_completely_missing", "debt_data_completely_missing")),
    "KELYA": ("not_evaluable", (), ("insufficient_years", "no_periods", "insufficient_years", "insufficient_years")),
    "LAKE": ("not_evaluable", (), ("net_debt_unreliable", "ebitda_non_positive", "ni_non_positive")),
    "LZB": ("not_evaluable", (), ("debt_data_completely_missing", "debt_data_completely_missing")),
    "MATW": ("not_evaluable", (), ("insufficient_years", "insufficient_years", "insufficient_years")),
    "MOV": ("not_evaluable", (), ("debt_data_completely_missing", "debt_data_completely_missing")),
    "NPK": ("not_evaluable", (), ("insufficient_years", "no_periods", "insufficient_years", "insufficient_years")),
    "PLOW": ("failed", ("cash_conversion",), ()),
    "ROCK": ("not_evaluable", (), ("debt_data_completely_missing", "debt_data_completely_missing")),
    "SCSC": ("failed", (), ()),
    "SGC": ("not_evaluable", (), ("ni_non_positive",)),
    "SMP": ("failed", (), ()),
    "SUPN": ("not_evaluable", (), ("debt_data_completely_missing", "debt_data_completely_missing", "ni_non_positive")),
    "UFPT": ("passed", (), ()),
    "USNA": ("passed", (), ()),
}

EXPECTED_DISTRIBUTION = {PASSED: 2, PASSED_WITH_FLAGS: 1, FAILED: 5, NOT_EVALUABLE: 15}


def pilot_tickers():
    return sorted(path.stem for path in SIMFIN_RAW_DIR.glob("*.json"))


def evaluate_pilot(ticker):
    """One pilot through the whole Stage-1 chain, exactly as a runner would wire it."""
    with open(SIMFIN_RAW_DIR / f"{ticker}.json", encoding="utf-8") as f:
        raw = json.load(f)
    periods = build_company_record(ticker, raw)["periods"]
    return evaluate_stage1_gates(
        roic=roic_3yr_average(periods),
        net_debt_ebitda=net_debt_ebitda_latest(periods),
        cash_conversion=cash_conversion_3yr_average(periods),
        net_income_margin=net_income_margin_3yr_average(periods),
        data_quality_flags=collect_data_quality_flags(periods),
    )


def evaluate_all_pilots():
    return {ticker: evaluate_pilot(ticker) for ticker in pilot_tickers()}


def distribution(gate_results):
    counts = Counter(result.status for result in gate_results.values())
    return {status: counts.get(status, 0) for status in (PASSED, PASSED_WITH_FLAGS, FAILED, NOT_EVALUABLE)}


def format_report(gate_results):
    def cell(value):
        return "    -" if value is None else f"{value:5.2f}"

    lines = [f"{'ticker':6} {'status':18} {'roic':>5} {'nd/ebitda':>9} {'cash_cv':>7} {'ni_mgn':>6}  flags / invalid_reasons"]
    for ticker, result in gate_results.items():
        detail = ",".join(result.flags) if result.status != NOT_EVALUABLE else ",".join(result.invalid_reasons)
        lines.append(
            f"{ticker:6} {result.status:18} {cell(result.roic):>5} {cell(result.net_debt_ebitda):>9} "
            f"{cell(result.cash_conversion):>7} {cell(result.net_income_margin):>6}  {detail}"
        )
    counts = distribution(gate_results)
    lines.append("")
    lines.append(
        f"{len(gate_results)} pilots: passed={counts[PASSED]}  passed_with_flags={counts[PASSED_WITH_FLAGS]}  "
        f"failed={counts[FAILED]}  not_evaluable={counts[NOT_EVALUABLE]}"
    )
    return "\n".join(lines)


def test_all_23_pilot_fixtures_are_covered_by_the_snapshot():
    assert len(pilot_tickers()) == 23
    assert set(pilot_tickers()) == set(EXPECTED_OUTCOMES)


def test_pilot_outcomes_match_the_snapshot():
    gate_results = evaluate_all_pilots()
    actual = {
        ticker: (result.status, result.flags, result.invalid_reasons) for ticker, result in gate_results.items()
    }
    assert actual == EXPECTED_OUTCOMES


def test_pilot_distribution_matches_the_snapshot():
    assert distribution(evaluate_all_pilots()) == EXPECTED_DISTRIBUTION


def test_pilot_run_round_trips_through_the_store_with_data_quality_flags(tmp_path):
    gate_results = evaluate_all_pilots()
    db_path = tmp_path / "pilots.db"
    save_run_results("2026-09-21T18:00:00", gate_results, db_path=db_path)

    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        survivors = {row["ticker"]: dict(row) for row in connection.execute("SELECT * FROM run_results")}
        eliminated = {row["ticker"]: dict(row) for row in connection.execute("SELECT * FROM eliminated_results")}
        runs_row = dict(connection.execute("SELECT * FROM runs").fetchone())
    finally:
        connection.close()

    # every pilot lands in exactly one table
    assert set(survivors) | set(eliminated) == set(gate_results)
    assert not set(survivors) & set(eliminated)
    assert set(survivors) == {ticker for ticker, _ in rank_companies(gate_results)}
    assert runs_row["companies_evaluated"] == 23
    assert runs_row["survivors_count"] == len(survivors)

    # the persisted flags are exactly what the adapter produced, in both tables
    for ticker, row in {**survivors, **eliminated}.items():
        assert row["data_quality_flags"] == ",".join(gate_results[ticker].data_quality_flags)
    assert "debt_data_completely_missing" in eliminated["ROCK"]["data_quality_flags"].split(",")
    assert "pl_lags_other_statements" in eliminated["SGC"]["data_quality_flags"].split(",")


if __name__ == "__main__":
    print(format_report(evaluate_all_pilots()))

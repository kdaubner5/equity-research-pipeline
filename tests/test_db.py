import csv
import datetime
import sqlite3

import pytest

from conftest import all_company_ids, load_company
from metrics_layer.cash_conversion import cash_conversion_3yr_average, net_income_margin_3yr_average
from metrics_layer.net_debt_ebitda import net_debt_ebitda_latest
from metrics_layer.result import MetricResult
from metrics_layer.roic import roic_3yr_average
from gates_layer.stage1_gates import evaluate_stage1_gates, rank_companies
from store_layer.db import export_survivors_csv, init_db, save_run_results


def _simulated_run_gate_results():
    """Gate results for all fixtures - the simulated run's input, per Issue #6."""
    gate_results = {}
    for company_id in all_company_ids():
        periods = load_company(company_id)["periods"]
        gate_results[company_id] = evaluate_stage1_gates(
            roic=roic_3yr_average(periods),
            net_debt_ebitda=net_debt_ebitda_latest(periods),
            cash_conversion=cash_conversion_3yr_average(periods),
            net_income_margin=net_income_margin_3yr_average(periods),
        )
    return gate_results


def _zero_survivor_gate_results():
    """Synthetic gate_results where every company fails - rank_companies() empty."""
    failing = evaluate_stage1_gates(
        roic=MetricResult(value=0.01),
        net_debt_ebitda=MetricResult(value=5.0),
        cash_conversion=MetricResult(value=0.1),
        net_income_margin=MetricResult(value=0.01),
    )
    assert failing.status == "failed"
    return {ticker: failing for ticker in ("ZERO01", "ZERO02", "ZERO03")}


def _query_run(db_path, run_timestamp):
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM run_results WHERE run_timestamp = ? ORDER BY rank_position",
            (run_timestamp,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _query_runs_table(db_path, run_timestamp):
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM runs WHERE run_timestamp = ?",
            (run_timestamp,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "archive.db"
    init_db(db_path)
    init_db(db_path)  # must not raise on a second call

    connection = sqlite3.connect(db_path)
    try:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('run_results', 'runs')"
        ).fetchall()
    finally:
        connection.close()
    # each table must exist exactly once - catches a regression that makes
    # either CREATE TABLE non-idempotent, not just "some table exists"
    table_names = [row[0] for row in tables]
    assert table_names.count("run_results") == 1
    assert table_names.count("runs") == 1


def test_save_run_results_only_survivors_are_archived(tmp_path):
    db_path = tmp_path / "archive.db"
    gate_results = _simulated_run_gate_results()
    run_timestamp = "2026-09-15T14:30:00"

    save_run_results(run_timestamp, gate_results, db_path=db_path)

    rows = _query_run(db_path, run_timestamp)
    archived_tickers = {row["ticker"] for row in rows}

    expected_survivors = {ticker for ticker, _ in rank_companies(gate_results)}
    assert archived_tickers == expected_survivors

    # sanity: a failed and a not_evaluable company from the fixture set must
    # be excluded, not just "some" tickers missing
    assert gate_results["GEKO12"].status == "failed"
    assert gate_results["NAKA17"].status == "not_evaluable"
    assert "GEKO12" not in archived_tickers
    assert "NAKA17" not in archived_tickers


def test_save_run_results_rank_position_matches_rank_companies(tmp_path):
    db_path = tmp_path / "archive.db"
    gate_results = _simulated_run_gate_results()
    run_timestamp = "2026-09-15T14:30:00"

    save_run_results(run_timestamp, gate_results, db_path=db_path)

    rows = _query_run(db_path, run_timestamp)
    rank_by_ticker = {row["ticker"]: row["rank_position"] for row in rows}

    expected = {ticker: position for position, (ticker, _) in enumerate(rank_companies(gate_results), start=1)}
    assert rank_by_ticker == expected
    # positions are a contiguous 1..N sequence, not reset per flag-count group
    assert sorted(rank_by_ticker.values()) == list(range(1, len(rank_by_ticker) + 1))


def test_save_run_results_two_timestamps_do_not_overwrite_each_other(tmp_path):
    db_path = tmp_path / "archive.db"
    gate_results = _simulated_run_gate_results()

    save_run_results("2026-09-15T14:30:00", gate_results, db_path=db_path)
    save_run_results("2026-09-16T09:00:00", gate_results, db_path=db_path)

    rows_day1 = _query_run(db_path, "2026-09-15T14:30:00")
    rows_day2 = _query_run(db_path, "2026-09-16T09:00:00")

    survivor_count = len(rank_companies(gate_results))
    assert len(rows_day1) == survivor_count
    assert len(rows_day2) == survivor_count
    assert {row["ticker"] for row in rows_day1} == {row["ticker"] for row in rows_day2}


def test_save_run_results_same_timestamp_twice_raises(tmp_path):
    db_path = tmp_path / "archive.db"
    gate_results = _simulated_run_gate_results()
    run_timestamp = "2026-09-15T14:30:00"

    save_run_results(run_timestamp, gate_results, db_path=db_path)

    # a forward-log is a historical record: re-saving the same run_timestamp
    # is a caller bug that must raise (composite primary key violation),
    # never silently overwrite the already-archived run
    with pytest.raises(sqlite3.IntegrityError):
        save_run_results(run_timestamp, gate_results, db_path=db_path)

    # the original rows must be untouched by the failed second attempt
    rows = _query_run(db_path, run_timestamp)
    assert len(rows) == len(rank_companies(gate_results))


def test_export_survivors_csv_matches_db_for_same_run(tmp_path):
    db_path = tmp_path / "archive.db"
    csv_dir = tmp_path / "runs"
    gate_results = _simulated_run_gate_results()
    run_timestamp = "2026-09-15T14:30:00"

    save_run_results(run_timestamp, gate_results, db_path=db_path)
    csv_path = export_survivors_csv(run_timestamp, gate_results, output_dir=csv_dir)

    db_rows = {row["ticker"]: row for row in _query_run(db_path, run_timestamp)}

    with open(csv_path, newline="", encoding="utf-8") as f:
        csv_rows = list(csv.DictReader(f))

    assert {row["ticker"] for row in csv_rows} == set(db_rows.keys())
    assert len(csv_rows) == len(db_rows)

    for csv_row in csv_rows:
        db_row = db_rows[csv_row["ticker"]]
        assert csv_row["status"] == db_row["status"]
        assert csv_row["flags"] == db_row["flags"]
        assert int(csv_row["rank_position"]) == db_row["rank_position"]
        assert float(csv_row["roic_3yr"]) == pytest.approx(db_row["roic_3yr"], abs=1e-9)
        assert float(csv_row["net_debt_ebitda"]) == pytest.approx(db_row["net_debt_ebitda"], abs=1e-9)
        assert float(csv_row["cash_conversion_3yr"]) == pytest.approx(db_row["cash_conversion_3yr"], abs=1e-9)


def test_export_survivors_csv_filename_strips_colons_from_timestamp(tmp_path):
    csv_dir = tmp_path / "runs"
    gate_results = _simulated_run_gate_results()

    csv_path = export_survivors_csv("2026-09-15T14:30:00", gate_results, output_dir=csv_dir)

    assert csv_path.name == "survivors_2026-09-15T143000.csv"
    assert csv_path.exists()


@pytest.mark.parametrize("func_name", ["save_run_results", "export_survivors_csv"])
def test_non_string_run_timestamp_raises_typeerror(tmp_path, func_name):
    # a raw datetime is an easy mistake given the parameter name "timestamp" -
    # must raise a clear TypeError, not a confusing one from datetime.replace()
    # silently shadowing the intended str.replace() in export_survivors_csv
    gate_results = _simulated_run_gate_results()
    run_timestamp = datetime.datetime(2026, 9, 15, 14, 30, 0)

    func = {"save_run_results": save_run_results, "export_survivors_csv": export_survivors_csv}[func_name]
    kwargs = {"db_path": tmp_path / "archive.db"} if func_name == "save_run_results" else {"output_dir": tmp_path / "runs"}

    with pytest.raises(TypeError):
        func(run_timestamp, gate_results, **kwargs)


def test_zero_survivor_run_still_writes_runs_row_and_header_only_csv(tmp_path):
    db_path = tmp_path / "archive.db"
    csv_dir = tmp_path / "runs"
    gate_results = _zero_survivor_gate_results()
    run_timestamp = "2026-09-15T08:00:00"
    assert rank_companies(gate_results) == []  # sanity: genuinely 0 survivors

    save_run_results(run_timestamp, gate_results, db_path=db_path)
    csv_path = export_survivors_csv(run_timestamp, gate_results, output_dir=csv_dir)

    run_row = _query_runs_table(db_path, run_timestamp)
    assert run_row is not None
    assert run_row["companies_evaluated"] == len(gate_results)
    assert run_row["survivors_count"] == 0

    assert _query_run(db_path, run_timestamp) == []

    assert csv_path.exists()
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == [
            "rank_position",
            "ticker",
            "status",
            "flags",
            "roic_3yr",
            "net_debt_ebitda",
            "cash_conversion_3yr",
        ]
        assert list(reader) == []  # header only, no data rows


def test_zero_survivor_run_and_survivor_run_coexist(tmp_path):
    db_path = tmp_path / "archive.db"
    csv_dir = tmp_path / "runs"

    zero_gate_results = _zero_survivor_gate_results()
    zero_timestamp = "2026-09-15T08:00:00"
    save_run_results(zero_timestamp, zero_gate_results, db_path=db_path)
    zero_csv_path = export_survivors_csv(zero_timestamp, zero_gate_results, output_dir=csv_dir)

    real_gate_results = _simulated_run_gate_results()
    real_timestamp = "2026-09-15T14:30:00"
    save_run_results(real_timestamp, real_gate_results, db_path=db_path)
    real_csv_path = export_survivors_csv(real_timestamp, real_gate_results, output_dir=csv_dir)

    zero_run_row = _query_runs_table(db_path, zero_timestamp)
    real_run_row = _query_runs_table(db_path, real_timestamp)
    assert zero_run_row["survivors_count"] == 0
    real_survivor_count = len(rank_companies(real_gate_results))
    assert real_survivor_count > 0
    assert real_run_row["survivors_count"] == real_survivor_count

    assert _query_run(db_path, zero_timestamp) == []
    assert len(_query_run(db_path, real_timestamp)) == real_survivor_count

    with open(zero_csv_path, newline="", encoding="utf-8") as f:
        assert list(csv.DictReader(f)) == []
    with open(real_csv_path, newline="", encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == real_survivor_count

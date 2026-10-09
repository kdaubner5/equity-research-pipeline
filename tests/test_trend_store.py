"""Trend-Guard archive tables and CSV (docs/decisions.md "Trend-Guard", Abschnitt 3 und 8, Test 4). Own
tables created with CREATE TABLE IF NOT EXISTS: no existing table and no Stage-1/Stage-2 code is touched.
Every path is tmp_path-based."""

import csv
import sqlite3

import pytest

from gates_layer.trend_guard import (
    MARK_MARGIN_FALLING,
    MARK_ROIC_BELOW_FLOOR,
    STATUS_CLEAR,
    STATUS_MARKED,
    STATUS_NOT_EVALUABLE,
    TrendResult,
    not_evaluable_trend_result,
)
from store_layer.db import init_db, save_run_results
from store_layer.stage2_store import init_stage2_tables
from store_layer.trend_store import TREND_CSV_FIELDNAMES, export_trend_csv, init_trend_tables, save_trend_results

RUN_TS = "2026-10-08T12:00:00"


def clear():
    return TrendResult(
        status=STATUS_CLEAR, marks=(), revenue_ratio=1.02, ebitda_margin_ratio=0.99, roic_last_year=0.15,
        revenue_invalid_reason=None, margin_invalid_reason=None, roic_invalid_reason=None,
    )


def marked():
    return TrendResult(
        status=STATUS_MARKED, marks=(MARK_MARGIN_FALLING, MARK_ROIC_BELOW_FLOOR), revenue_ratio=0.95,
        ebitda_margin_ratio=0.66, roic_last_year=None, revenue_invalid_reason=None, margin_invalid_reason=None,
        roic_invalid_reason="net_debt_unreliable",
    )


def save(db_path, results, **kwargs):
    defaults = dict(
        stage1_run_timestamp="2026-09-30T18:53:07", prefilter_run_timestamp="2026-09-24T18:13:48", as_of="2026-09-24",
        config_hash="abc123", simfin_ids={t: i for i, t in enumerate(sorted(results), start=1)}, db_path=db_path,
    )
    defaults.update(kwargs)
    save_trend_results(RUN_TS, results=results, **defaults, rule_state=None)


def rows(db_path, sql):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_init_creates_the_tables_and_is_idempotent(tmp_path):
    db = tmp_path / "a.db"
    init_trend_tables(db)
    init_trend_tables(db)
    names = {r[0] for r in rows(db, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"trend_runs", "trend_results"} <= names


def test_init_leaves_existing_stage1_and_stage2_tables_untouched(tmp_path):
    db = tmp_path / "a.db"
    init_db(db)
    init_stage2_tables(db)
    save_run_results("2026-09-30T18:53:07", {}, config_hash="x", db_path=db, rule_state=None)
    watched = "('runs','run_results','eliminated_results','universe_prefilter','stage2_runs','stage2_results')"
    before = rows(db, "SELECT * FROM runs"), rows(db, f"SELECT name, sql FROM sqlite_master WHERE name IN {watched}")
    init_trend_tables(db)
    assert (rows(db, "SELECT * FROM runs"), rows(db, f"SELECT name, sql FROM sqlite_master WHERE name IN {watched}")) == before


def test_round_trip_one_row_per_company(tmp_path):
    db = tmp_path / "a.db"
    results = {"AAA": clear(), "BBB": marked(), "CCC": not_evaluable_trend_result("fundamentals_cache_missing")}
    save(db, results)
    got = {
        r[0]: r
        for r in rows(
            db,
            "SELECT ticker, status, marks, mark_count, revenue_ratio, ebitda_margin_ratio, roic_last_year, "
            "revenue_invalid_reason, margin_invalid_reason, roic_invalid_reason, simfin_id FROM trend_results",
        )
    }
    assert set(got) == {"AAA", "BBB", "CCC"}
    assert got["AAA"][1:7] == (STATUS_CLEAR, "", 0, 1.02, 0.99, 0.15)
    assert got["BBB"][1:4] == (STATUS_MARKED, f"{MARK_MARGIN_FALLING},{MARK_ROIC_BELOW_FLOOR}", 2)
    assert got["BBB"][6] is None and got["BBB"][9] == "net_debt_unreliable"  # a missing value stays NULL, never 0
    assert got["CCC"][1] == STATUS_NOT_EVALUABLE and got["CCC"][4:7] == (None, None, None)
    assert got["CCC"][7:10] == ("fundamentals_cache_missing",) * 3
    assert got["AAA"][10] == 1 and got["CCC"][10] == 3


def test_run_row_records_provenance_and_counts(tmp_path):
    db = tmp_path / "a.db"
    save(db, {"AAA": clear(), "BBB": marked()})
    assert rows(
        db,
        "SELECT run_timestamp, stage1_run_timestamp, prefilter_run_timestamp, as_of, config_hash, companies_evaluated, "
        "marked_count FROM trend_runs",
    ) == [(RUN_TS, "2026-09-30T18:53:07", "2026-09-24T18:13:48", "2026-09-24", "abc123", 2, 1)]


def test_a_run_without_any_result_still_leaves_its_run_row(tmp_path):
    db = tmp_path / "a.db"
    save(db, {})
    assert rows(db, "SELECT companies_evaluated, marked_count FROM trend_runs") == [(0, 0)]


def test_a_repeated_run_timestamp_is_refused_and_writes_nothing(tmp_path):
    db = tmp_path / "a.db"
    save(db, {"AAA": clear()})
    with pytest.raises(sqlite3.IntegrityError):
        save(db, {"BBB": clear()})
    assert rows(db, "SELECT ticker FROM trend_results") == [("AAA",)]


def test_a_failing_row_rolls_the_whole_run_back(tmp_path):
    db = tmp_path / "a.db"
    init_trend_tables(db)
    bad = clear()
    object.__setattr__(bad, "marks", 5)  # ",".join(5) raises TypeError mid-write
    with pytest.raises(TypeError):
        save(db, {"AAA": clear(), "BBB": bad})
    assert rows(db, "SELECT COUNT(*) FROM trend_runs") == [(0,)]
    assert rows(db, "SELECT COUNT(*) FROM trend_results") == [(0,)]


def test_a_company_without_a_known_simfin_id_stores_null(tmp_path):
    db = tmp_path / "a.db"
    save(db, {"AAA": clear()}, simfin_ids={})
    assert rows(db, "SELECT ticker, simfin_id FROM trend_results") == [("AAA", None)]


def test_run_timestamp_must_be_a_string(tmp_path):
    with pytest.raises(TypeError):
        save_trend_results(
            object(), results={}, stage1_run_timestamp="x", prefilter_run_timestamp="y", as_of="2026-09-24",
            config_hash="c", simfin_ids={}, db_path=tmp_path / "a.db", rule_state=None,
        )


def test_saving_does_not_change_stage1_rows(tmp_path):
    db = tmp_path / "a.db"
    init_db(db)
    save_run_results("2026-09-30T18:53:07", {}, config_hash="x", db_path=db, rule_state=None)
    before = rows(db, "SELECT * FROM runs") + rows(db, "SELECT * FROM run_results")
    save(db, {"AAA": clear()})
    assert rows(db, "SELECT * FROM runs") + rows(db, "SELECT * FROM run_results") == before


def test_csv_lists_every_company_sorted_by_ticker(tmp_path):
    results = {"BBB": marked(), "AAA": clear(), "CCC": not_evaluable_trend_result("no_periods")}
    path = export_trend_csv(RUN_TS, results, output_dir=tmp_path)
    assert path.parent == tmp_path and "2026-10-08" in path.name and ":" not in path.name
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == TREND_CSV_FIELDNAMES
        data = list(reader)
    assert [r["ticker"] for r in data] == ["AAA", "BBB", "CCC"]
    assert data[1]["marks"] == f"{MARK_MARGIN_FALLING},{MARK_ROIC_BELOW_FLOOR}" and data[1]["mark_count"] == "2"
    assert data[1]["roic_last_year"] == "" and data[1]["roic_invalid_reason"] == "net_debt_unreliable"
    assert data[2]["status"] == STATUS_NOT_EVALUABLE


def test_csv_with_no_company_is_header_only(tmp_path):
    path = export_trend_csv(RUN_TS, {}, output_dir=tmp_path)
    assert path.read_text(encoding="utf-8").strip().split(",") == TREND_CSV_FIELDNAMES


def test_csv_never_overwrites_an_earlier_file(tmp_path):
    export_trend_csv(RUN_TS, {}, output_dir=tmp_path)
    with pytest.raises(FileExistsError):
        export_trend_csv(RUN_TS, {}, output_dir=tmp_path)

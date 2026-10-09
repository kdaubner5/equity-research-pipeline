"""Stage-2 archive tables and CSV (docs/decisions.md "Stufe 2 - Beantwortung Q1-Q9 und Runner",
Entscheidung R2). Own tables, created with CREATE TABLE IF NOT EXISTS: no existing table and no
Stage-1 code is touched. Every path is tmp_path-based."""

import csv
import sqlite3

import pytest

from gates_layer.stage1_gates import FAILED, NOT_EVALUABLE, PASSED, PASSED_WITH_FLAGS
from gates_layer.stage2_gates import Stage2Result
from store_layer.db import init_db, save_run_results
from store_layer.stage2_store import (
    STAGE2_CSV_FIELDNAMES,
    export_stage2_csv,
    init_stage2_tables,
    save_stage2_results,
)

RUN_TS = "2026-10-06T12:00:00"


def result(status=PASSED, *, flags=(), invalid=(), percentile=0.1, level="fine", info=(), ev=6.0, fcf=0.08):
    return Stage2Result(
        status=status,
        flags=tuple(flags),
        invalid_reasons=tuple(invalid),
        ev_ebitda=ev,
        ev_ebitda_percentile=percentile,
        peer_level=level,
        peer_group=107002 if level == "fine" else None,
        peer_rank=2 if level == "fine" else None,
        peer_n=10 if level == "fine" else None,
        peer_n_fine=10,
        peer_n_coarse=12,
        fcf_yield=fcf,
        ev_ebitda_normalized=7.5,
        ev_ebitda_normalized_invalid_reason=None,
        info_flags=tuple(info),
    )


def save(db_path, results, ranked, **kwargs):
    defaults = dict(
        stage1_run_timestamp="2026-09-30T17:38:53",
        prefilter_run_timestamp="2026-09-24T18:13:48",
        as_of="2026-09-24",
        config_hash="abc123",
        peer_base_size=77,
        simfin_ids={t: i for i, t in enumerate(sorted(results), start=1)},
        db_path=db_path,
    )
    defaults.update(kwargs)
    save_stage2_results(RUN_TS, results=results, ranked=ranked, **defaults, rule_state=None)


def rows(db_path, sql):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_init_creates_the_tables_and_is_idempotent(tmp_path):
    db = tmp_path / "a.db"
    init_stage2_tables(db)
    init_stage2_tables(db)
    names = {r[0] for r in rows(db, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"stage2_runs", "stage2_results"} <= names


def test_init_leaves_existing_stage1_tables_and_rows_untouched(tmp_path):
    db = tmp_path / "a.db"
    init_db(db)
    save_run_results("2026-09-30T17:38:53", {}, config_hash="x", db_path=db, rule_state=None)
    before = rows(db, "SELECT * FROM runs")
    schema_before = rows(db, "SELECT name, sql FROM sqlite_master WHERE name IN ('runs','run_results','eliminated_results','universe_prefilter')")
    init_stage2_tables(db)
    assert rows(db, "SELECT * FROM runs") == before
    assert rows(
        db, "SELECT name, sql FROM sqlite_master WHERE name IN ('runs','run_results','eliminated_results','universe_prefilter')"
    ) == schema_before


def test_round_trip_one_row_per_result_with_rank_only_for_ranked_companies(tmp_path):
    db = tmp_path / "a.db"
    results = {
        "AAA": result(PASSED, percentile=0.0),
        "BBB": result(PASSED_WITH_FLAGS, flags=("fcf_yield",), info=("cyclical_sector", "peer_group_coarse")),
        "CCC": result(FAILED, invalid=("capex_missing",), percentile=0.9),
        "DDD": result(NOT_EVALUABLE, invalid=("fundamentals_cache_missing",), ev=None, fcf=None, percentile=None, level=None),
    }
    ranked = [("AAA", results["AAA"]), ("BBB", results["BBB"])]
    save(db, results, ranked)
    got = {
        r[0]: r
        for r in rows(
            db,
            "SELECT ticker, status, flags, invalid_reasons, info_flags, rank_position, ev_ebitda, peer_level, simfin_id "
            "FROM stage2_results ORDER BY ticker",
        )
    }
    assert set(got) == {"AAA", "BBB", "CCC", "DDD"}
    assert got["AAA"][1:6] == (PASSED, "", "", "", 1)
    assert got["BBB"][1:6] == (PASSED_WITH_FLAGS, "fcf_yield", "", "cyclical_sector,peer_group_coarse", 2)
    assert got["CCC"][3] == "capex_missing" and got["CCC"][5] is None
    assert got["DDD"][6] is None and got["DDD"][7] is None  # a missing value stays NULL, never 0
    assert got["AAA"][8] == 1 and got["DDD"][8] == 4


def test_run_row_records_the_provenance_and_counts(tmp_path):
    db = tmp_path / "a.db"
    results = {"AAA": result(PASSED), "CCC": result(FAILED)}
    save(db, results, [("AAA", results["AAA"])])
    assert rows(
        db,
        "SELECT run_timestamp, stage1_run_timestamp, prefilter_run_timestamp, as_of, config_hash, "
        "companies_evaluated, ranked_count, peer_base_size FROM stage2_runs",
    ) == [(RUN_TS, "2026-09-30T17:38:53", "2026-09-24T18:13:48", "2026-09-24", "abc123", 2, 1, 77)]


def test_a_run_without_any_result_still_leaves_its_run_row(tmp_path):
    db = tmp_path / "a.db"
    save(db, {}, [])
    assert rows(db, "SELECT companies_evaluated, ranked_count FROM stage2_runs") == [(0, 0)]
    assert rows(db, "SELECT COUNT(*) FROM stage2_results") == [(0,)]


def test_a_second_save_with_the_same_run_timestamp_is_refused_and_writes_nothing(tmp_path):
    db = tmp_path / "a.db"
    save(db, {"AAA": result()}, [])
    with pytest.raises(sqlite3.IntegrityError):
        save(db, {"BBB": result()}, [])
    assert rows(db, "SELECT ticker FROM stage2_results") == [("AAA",)]
    assert rows(db, "SELECT COUNT(*) FROM stage2_runs") == [(1,)]


def test_a_failing_row_rolls_the_whole_run_back(tmp_path):
    db = tmp_path / "a.db"
    init_stage2_tables(db)
    bad = result()
    object.__setattr__(bad, "info_flags", 5)  # ",".join(5) raises TypeError mid-write
    with pytest.raises(TypeError):
        save(db, {"AAA": result(), "BBB": bad}, [])
    assert rows(db, "SELECT COUNT(*) FROM stage2_runs") == [(0,)]
    assert rows(db, "SELECT COUNT(*) FROM stage2_results") == [(0,)]


def test_a_ranked_company_without_a_result_is_a_caller_error_and_writes_nothing(tmp_path):
    db = tmp_path / "a.db"
    with pytest.raises(ValueError, match="ZZZ"):
        save(db, {"AAA": result()}, [("ZZZ", result())])
    assert not db.exists() or rows(db, "SELECT COUNT(*) FROM stage2_runs") == [(0,)]


def test_a_result_without_a_known_simfin_id_stores_null_not_a_made_up_id(tmp_path):
    # a Stage-1 survivor that is not in the pre-filtered universe has no SimFin id here
    db = tmp_path / "a.db"
    save(db, {"AAA": result()}, [], simfin_ids={})
    assert rows(db, "SELECT ticker, simfin_id FROM stage2_results") == [("AAA", None)]


def test_run_timestamp_must_be_a_string(tmp_path):
    with pytest.raises(TypeError):
        save_stage2_results(
            object(), results={}, ranked=[], stage1_run_timestamp="x", prefilter_run_timestamp="y",
            as_of="2026-09-24", config_hash="c", peer_base_size=0, simfin_ids={}, db_path=tmp_path / "a.db", rule_state=None,
        )


def test_csv_lists_the_ranked_companies_in_rank_order(tmp_path):
    ranked = [
        ("AAA", result(PASSED, percentile=0.0)),
        ("BBB", result(PASSED_WITH_FLAGS, flags=("ev_ebitda", "fcf_yield"), info=("peer_group_absolute",), level="absolute", percentile=None)),
    ]
    path = export_stage2_csv(RUN_TS, ranked, output_dir=tmp_path)
    assert path.parent == tmp_path and "2026-10-06" in path.name and ":" not in path.name
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == STAGE2_CSV_FIELDNAMES
        data = list(reader)
    assert [r["ticker"] for r in data] == ["AAA", "BBB"]
    assert [r["rank_position"] for r in data] == ["1", "2"]
    assert data[1]["flags"] == "ev_ebitda,fcf_yield"
    assert data[1]["ev_ebitda_percentile"] == "" and data[1]["peer_level"] == "absolute"
    assert data[1]["info_flags"] == "peer_group_absolute"


def test_csv_with_no_ranked_company_is_header_only(tmp_path):
    path = export_stage2_csv(RUN_TS, [], output_dir=tmp_path)
    assert path.read_text(encoding="utf-8").strip().split(",") == STAGE2_CSV_FIELDNAMES


def test_csv_never_overwrites_an_earlier_file(tmp_path):
    export_stage2_csv(RUN_TS, [], output_dir=tmp_path)
    with pytest.raises(FileExistsError):
        export_stage2_csv(RUN_TS, [], output_dir=tmp_path)


def test_saving_does_not_change_stage1_rows(tmp_path):
    db = tmp_path / "a.db"
    init_db(db)
    save_run_results("2026-09-30T17:38:53", {}, config_hash="x", db_path=db, rule_state=None)
    before = rows(db, "SELECT * FROM runs") + rows(db, "SELECT * FROM run_results")
    save(db, {"AAA": result()}, [])
    assert rows(db, "SELECT * FROM runs") + rows(db, "SELECT * FROM run_results") == before

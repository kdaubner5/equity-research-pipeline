"""Rule state and close price in the archive (docs/decisions.md "Regelstand und Kurs je Lauf im Archiv", Punkt 4 bis 6 und 8):
the four save functions take the rule state as a required keyword, write it in the run's own transaction (all or nothing),
universe_prefilter gets close_price_usd / shares_outstanding, runs gets prefilter_run_timestamp, an older archive is
migrated without losing anything. Every path is tmp_path-based; the real archive is never opened."""

import datetime
import sqlite3

import pytest

from gates_layer import universe_prefilter as pf
from store_layer.db import init_db, save_run_results, save_universe_prefilter
from store_layer.rule_state import RULE_FILES, RuleFile, RuleState, write_rule_state
from store_layer.stage2_store import save_stage2_results
from store_layer.trend_store import save_trend_results
from test_db import _simulated_run_gate_results
from test_stage2_store import result as stage2_result
from test_trend_store import clear

PF_TS = "2026-09-24T14:30:00"


def a_state(commit="a" * 40, code_state="clean", dirty=()):
    files = tuple(RuleFile(path=p, sha256=f"{i:064x}", content=f"k{i}: {i}\n") for i, p in enumerate(RULE_FILES))
    return RuleState(git_commit=commit, code_state=code_state, dirty_paths=tuple(dirty), files=files)


def q(db, sql, *params):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def rule_rows(db):
    """Number of run_rule_state rows; 0 when the table does not exist (nothing was ever written)."""
    names = {r[0] for r in q(db, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    return q(db, "SELECT COUNT(*) FROM run_rule_state")[0][0] if "run_rule_state" in names else 0


def pf_result(simfin_id, ticker="ABC", close=12.5, shares=8_000_000.0):
    cap = close * shares if close is not None and shares is not None else None
    return pf.PrefilterResult(
        simfin_id, ticker, 106005, pf.PASSED, None, cap, 1e7, 60, datetime.date(2026, 9, 23),
        close_price_usd=close, shares_outstanding=shares,
    )


# --- universe_prefilter ----------------------------------------------------------------------------------------


def test_prefilter_stores_close_and_shares_and_the_rule_state(tmp_path):
    db = tmp_path / "a.db"
    save_universe_prefilter(PF_TS, [pf_result(1), pf_result(2, "XYZ", close=3.0, shares=None)], config_hash="h", rule_state=a_state(), db_path=db)
    assert q(db, "SELECT simfin_id, close_price_usd, shares_outstanding FROM universe_prefilter ORDER BY simfin_id") == [
        (1, 12.5, 8_000_000.0),
        (2, 3.0, None),
    ]
    assert q(db, "SELECT step, run_timestamp, git_commit, code_state FROM run_rule_state") == [("prefilter", PF_TS, "a" * 40, "clean")]
    assert q(db, "SELECT COUNT(*) FROM run_rule_files")[0][0] == len(RULE_FILES)


def test_the_rule_state_is_a_required_keyword(tmp_path):
    with pytest.raises(TypeError):
        save_universe_prefilter(PF_TS, [pf_result(1)], config_hash="h", db_path=tmp_path / "a.db")
    with pytest.raises(TypeError):
        save_run_results("2026-09-30T10:00:00", {}, config_hash="h", db_path=tmp_path / "a.db")


def test_none_writes_no_rule_state_rows_but_the_results(tmp_path):
    db = tmp_path / "a.db"
    save_universe_prefilter(PF_TS, [pf_result(1)], config_hash="h", rule_state=None, db_path=db)
    assert q(db, "SELECT COUNT(*) FROM universe_prefilter")[0][0] == 1
    assert rule_rows(db) == 0


def test_a_failing_rule_state_write_leaves_no_prefilter_rows(tmp_path):
    db = tmp_path / "a.db"
    save_universe_prefilter("2026-09-01T10:00:00", [pf_result(1)], config_hash="h", rule_state=a_state(), db_path=db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO run_rule_state VALUES ('prefilter', ?, NULL, 'unknown', NULL)", (PF_TS,))  # blocks the later write
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.IntegrityError):
        save_universe_prefilter(PF_TS, [pf_result(1)], config_hash="h", rule_state=a_state(), db_path=db)
    assert q(db, "SELECT COUNT(*) FROM universe_prefilter WHERE run_timestamp = ?", PF_TS)[0][0] == 0


def test_a_repeated_prefilter_run_changes_nothing(tmp_path):
    db = tmp_path / "a.db"
    save_universe_prefilter(PF_TS, [pf_result(1)], config_hash="h", rule_state=a_state(), db_path=db)
    with pytest.raises(sqlite3.IntegrityError):
        save_universe_prefilter(PF_TS, [pf_result(1)], config_hash="h", rule_state=a_state(), db_path=db)
    assert q(db, "SELECT COUNT(*) FROM run_rule_state")[0][0] == 1


# --- migration of an older archive -----------------------------------------------------------------------------


OLD_RUNS = "CREATE TABLE runs (run_timestamp TEXT PRIMARY KEY, companies_evaluated INTEGER NOT NULL, survivors_count INTEGER NOT NULL, config_hash TEXT)"
OLD_PREFILTER = (
    "CREATE TABLE universe_prefilter (run_timestamp TEXT NOT NULL, simfin_id INTEGER NOT NULL, ticker TEXT, sector_code INTEGER, "
    "market_cap_usd REAL, median_dollar_volume_usd REAL, median_window_rows INTEGER, price_date TEXT, status TEXT NOT NULL, "
    "reason TEXT, config_hash TEXT NOT NULL, PRIMARY KEY (run_timestamp, simfin_id))"
)


def test_an_older_archive_is_migrated_and_keeps_its_rows(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(OLD_RUNS)
    conn.execute(OLD_PREFILTER)
    conn.execute("INSERT INTO runs VALUES ('2026-09-01T10:00:00', 5, 2, 'h0')")
    conn.execute("INSERT INTO universe_prefilter VALUES ('2026-09-01T09:00:00', 7, 'OLD', 1, 2e8, 1e7, 60, '2026-08-31', 'passed', NULL, 'h0')")
    conn.commit()
    conn.close()

    init_db(db)
    init_db(db)  # a second call is a no-op

    assert q(db, "SELECT run_timestamp, companies_evaluated, survivors_count, config_hash, prefilter_run_timestamp FROM runs") == [
        ("2026-09-01T10:00:00", 5, 2, "h0", None)
    ]
    assert q(db, "SELECT simfin_id, ticker, market_cap_usd, config_hash, close_price_usd, shares_outstanding FROM universe_prefilter") == [
        (7, "OLD", 2e8, "h0", None, None)
    ]


def test_a_new_archive_has_the_same_columns_as_a_migrated_one(tmp_path):
    new = tmp_path / "new.db"
    init_db(new)
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.execute(OLD_RUNS)
    conn.execute(OLD_PREFILTER)
    conn.commit()
    conn.close()
    init_db(old)
    for table in ("runs", "universe_prefilter"):
        assert [r[1] for r in q(new, f"PRAGMA table_info({table})")] == [r[1] for r in q(old, f"PRAGMA table_info({table})")]


# --- stage 1 ---------------------------------------------------------------------------------------------------


def test_stage1_stores_the_prefilter_reference_and_the_rule_state(tmp_path):
    db = tmp_path / "a.db"
    results = _simulated_run_gate_results()
    save_run_results("2026-09-30T10:00:00", results, config_hash="h", rule_state=a_state(code_state="dirty", dirty=("src/x.py",)),
                     prefilter_run_timestamp=PF_TS, db_path=db)
    assert q(db, "SELECT prefilter_run_timestamp FROM runs") == [(PF_TS,)]
    assert q(db, "SELECT step, code_state, dirty_paths FROM run_rule_state") == [("stage1", "dirty", "src/x.py")]


def test_stage1_without_a_prefilter_reference_stores_null(tmp_path):
    db = tmp_path / "a.db"
    save_run_results("2026-09-30T10:00:00", _simulated_run_gate_results(), config_hash="h", rule_state=None, db_path=db)
    assert q(db, "SELECT prefilter_run_timestamp FROM runs") == [(None,)]


def test_stage1_results_are_identical_with_and_without_a_rule_state(tmp_path):
    """The existing columns do not change because of the new ones (golden-master idea on a copy)."""
    results = _simulated_run_gate_results()
    with_state, without = tmp_path / "w.db", tmp_path / "n.db"
    save_run_results("2026-09-30T10:00:00", results, config_hash="h", rule_state=a_state(), db_path=with_state)
    save_run_results("2026-09-30T10:00:00", results, config_hash="h", rule_state=None, db_path=without)
    for table in ("runs", "run_results", "eliminated_results"):
        columns = ", ".join(r[1] for r in q(without, f"PRAGMA table_info({table})") if r[1] != "prefilter_run_timestamp")
        assert q(with_state, f"SELECT {columns} FROM {table} ORDER BY 1, 2") == q(without, f"SELECT {columns} FROM {table} ORDER BY 1, 2")


def test_a_failing_rule_state_write_leaves_no_stage1_rows(tmp_path):
    db = tmp_path / "a.db"
    init_db(db)
    conn = sqlite3.connect(db)
    write_rule_state(conn, "stage1", "2026-09-30T10:00:00", a_state())
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.IntegrityError):
        save_run_results("2026-09-30T10:00:00", _simulated_run_gate_results(), config_hash="h", rule_state=a_state(), db_path=db)
    assert q(db, "SELECT COUNT(*) FROM runs")[0][0] == 0
    assert q(db, "SELECT COUNT(*) FROM run_results")[0][0] == 0
    assert q(db, "SELECT COUNT(*) FROM eliminated_results")[0][0] == 0


# --- stage 2 and trend guard -----------------------------------------------------------------------------------


STAGE2_KW = dict(
    stage1_run_timestamp="2026-09-30T17:38:53", prefilter_run_timestamp=PF_TS, as_of="2026-09-24", config_hash="abc",
    peer_base_size=3, simfin_ids={"AAA": 1},
)


def test_stage2_stores_the_rule_state(tmp_path):
    db = tmp_path / "a.db"
    save_stage2_results("2026-10-06T12:00:00", results={"AAA": stage2_result()}, ranked=[("AAA", stage2_result())], rule_state=a_state(), db_path=db, **STAGE2_KW)
    assert q(db, "SELECT step, run_timestamp, code_state FROM run_rule_state") == [("stage2", "2026-10-06T12:00:00", "clean")]


def test_stage2_rule_state_is_required_and_atomic(tmp_path):
    db = tmp_path / "a.db"
    with pytest.raises(TypeError):
        save_stage2_results("2026-10-06T12:00:00", results={"AAA": stage2_result()}, ranked=[], db_path=db, **STAGE2_KW)
    save_stage2_results("2026-10-06T12:00:00", results={"AAA": stage2_result()}, ranked=[], rule_state=None, db_path=db, **STAGE2_KW)
    with pytest.raises(sqlite3.IntegrityError):
        save_stage2_results("2026-10-06T12:00:00", results={"AAA": stage2_result()}, ranked=[], rule_state=a_state(), db_path=db, **STAGE2_KW)
    assert rule_rows(db) == 0


def test_a_failing_rule_state_write_leaves_no_stage2_rows(tmp_path):
    db = tmp_path / "a.db"
    save_stage2_results("2026-10-06T11:00:00", results={"AAA": stage2_result()}, ranked=[], rule_state=a_state(), db_path=db, **STAGE2_KW)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO run_rule_state VALUES ('stage2', '2026-10-06T12:00:00', NULL, 'unknown', NULL)")
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.IntegrityError):
        save_stage2_results("2026-10-06T12:00:00", results={"AAA": stage2_result()}, ranked=[], rule_state=a_state(), db_path=db, **STAGE2_KW)
    assert q(db, "SELECT COUNT(*) FROM stage2_runs WHERE run_timestamp = '2026-10-06T12:00:00'")[0][0] == 0


TREND_KW = dict(
    stage1_run_timestamp="2026-09-30T18:53:07", prefilter_run_timestamp=PF_TS, as_of="2026-09-24", config_hash="abc", simfin_ids={"AAA": 1},
)


def test_trend_stores_the_rule_state_and_requires_it(tmp_path):
    db = tmp_path / "a.db"
    with pytest.raises(TypeError):
        save_trend_results("2026-10-08T12:00:00", results={"AAA": clear()}, db_path=db, **TREND_KW)
    save_trend_results("2026-10-08T12:00:00", results={"AAA": clear()}, rule_state=a_state(), db_path=db, **TREND_KW)
    assert q(db, "SELECT step, code_state FROM run_rule_state") == [("trend", "clean")]


def test_a_failing_rule_state_write_leaves_no_trend_rows(tmp_path):
    db = tmp_path / "a.db"
    save_trend_results("2026-10-08T11:00:00", results={"AAA": clear()}, rule_state=a_state(), db_path=db, **TREND_KW)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO run_rule_state VALUES ('trend', '2026-10-08T12:00:00', NULL, 'unknown', NULL)")
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.IntegrityError):
        save_trend_results("2026-10-08T12:00:00", results={"AAA": clear()}, rule_state=a_state(), db_path=db, **TREND_KW)
    assert q(db, "SELECT COUNT(*) FROM trend_runs WHERE run_timestamp = '2026-10-08T12:00:00'")[0][0] == 0

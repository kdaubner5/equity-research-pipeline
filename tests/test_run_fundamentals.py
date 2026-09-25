"""End-to-end tests for run_fundamentals.py (Issue #43): the whole chain
(survivors query, batch fetch, cache) against a scripted fake SimFin -
offline and deterministic, no live call, no real waiting."""

import io
import json
import sqlite3

import pytest

import run_fundamentals as rf
from data_layer import simfin_fundamentals as sf
from gates_layer.universe_prefilter import PASSED, EXCLUDED, PrefilterResult
from store_layer.db import save_universe_prefilter

from universe_fakes import (  # noqa: F401  (clock is a fixture)
    FakeSimFin,
    clock,
    statements_record,
)

RUN_TS = "2026-09-24T18:13:48"


def passed(simfin_id, ticker):
    return PrefilterResult(
        simfin_id=simfin_id,
        ticker=ticker,
        sector_code=106,
        status=PASSED,
        reason=None,
        market_cap_usd=1_000_000_000,
        median_dollar_volume_usd=2_000_000,
        median_window_rows=60,
        price_date=None,
    )


def excluded(simfin_id, ticker, reason="market_cap_below_min"):
    return PrefilterResult(
        simfin_id=simfin_id,
        ticker=ticker,
        sector_code=106,
        status=EXCLUDED,
        reason=reason,
        market_cap_usd=1_000,
        median_dollar_volume_usd=None,
        median_window_rows=None,
        price_date=None,
    )


def seed_db(db_path, *, run_timestamp=RUN_TS, survivor_ids=(1, 2, 3), excluded_ids=()):
    results = [passed(i, f"T{i}") for i in survivor_ids] + [excluded(i, f"X{i}") for i in excluded_ids]
    save_universe_prefilter(run_timestamp, results, config_hash="deadbeef", db_path=db_path)
    return [i for i in survivor_ids]


def do_run(tmp_path, clock, statements_by_id=None, **overrides):
    out = io.StringIO()
    kwargs = dict(
        db_path=tmp_path / "archive.db",
        cache_dir=tmp_path / "cache",
        api_key="test-key",
        sleep=clock.sleep,
        clock=clock,
        out=out,
    )
    kwargs.update(overrides)
    return rf.run(**kwargs), out.getvalue()


# --- load_survivor_ids -----------------------------------------------------------


def test_load_survivor_ids_returns_only_passed_rows_ascending_by_id(tmp_path):
    db_path = tmp_path / "archive.db"
    seed_db(db_path, survivor_ids=(3, 1, 2), excluded_ids=(4,))
    ts, rows = rf.load_survivor_ids(db_path)
    assert ts == RUN_TS
    assert rows == [(1, "T1"), (2, "T2"), (3, "T3")]


def test_load_survivor_ids_with_an_explicit_run_timestamp_selects_that_run(tmp_path):
    db_path = tmp_path / "archive.db"
    seed_db(db_path, run_timestamp="2026-09-24T18:13:48", survivor_ids=(1,))
    seed_db(db_path, run_timestamp="2026-09-25T09:00:00", survivor_ids=(2,))
    ts, rows = rf.load_survivor_ids(db_path, run_timestamp="2026-09-24T18:13:48")
    assert ts == "2026-09-24T18:13:48" and rows == [(1, "T1")]


def test_load_survivor_ids_default_is_the_latest_run_with_passed_rows(tmp_path):
    db_path = tmp_path / "archive.db"
    seed_db(db_path, run_timestamp="2026-09-24T18:13:48", survivor_ids=(1,))
    seed_db(db_path, run_timestamp="2026-09-25T09:00:00", survivor_ids=(2,))
    ts, rows = rf.load_survivor_ids(db_path)
    assert ts == "2026-09-25T09:00:00" and rows == [(2, "T2")]


def test_load_survivor_ids_raises_if_the_database_is_missing(tmp_path):
    with pytest.raises(rf.FundamentalsRunError, match="not found"):
        rf.load_survivor_ids(tmp_path / "nope.db")


def test_load_survivor_ids_raises_if_no_run_has_any_passed_row(tmp_path):
    db_path = tmp_path / "archive.db"
    seed_db(db_path, survivor_ids=(), excluded_ids=(4,))
    with pytest.raises(rf.FundamentalsRunError, match="no universe_prefilter run"):
        rf.load_survivor_ids(db_path)


def test_load_survivor_ids_raises_for_an_explicit_run_timestamp_with_no_passed_rows(tmp_path):
    db_path = tmp_path / "archive.db"
    seed_db(db_path, run_timestamp="2026-09-24T18:13:48", survivor_ids=(1,))
    with pytest.raises(rf.FundamentalsRunError, match="has no passed rows"):
        rf.load_survivor_ids(db_path, run_timestamp="2026-09-01T00:00:00")


# --- run(): happy path, caching, resumability -------------------------------------


def test_a_complete_run_caches_every_survivor_id_and_reports_complete(monkeypatch, tmp_path, clock):
    seed_db(tmp_path / "archive.db", survivor_ids=(1, 2, 3))
    FakeSimFin(monkeypatch, statements={i: statements_record(i) for i in (1, 2, 3)})
    code, output = do_run(tmp_path, clock)
    assert code == rf.EXIT_OK
    assert "complete=True" in output and "ok=3" in output
    for simfin_id in (1, 2, 3):
        assert sf.cache_path(tmp_path / "cache" / "2026-09-24", simfin_id).exists()


def test_run_fundamentals_writes_nothing_to_run_archive_db(monkeypatch, tmp_path, clock):
    db_path = tmp_path / "archive.db"
    seed_db(db_path, survivor_ids=(1, 2))
    FakeSimFin(monkeypatch, statements={i: statements_record(i) for i in (1, 2)})
    before = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM universe_prefilter").fetchone()[0]
    do_run(tmp_path, clock)
    after = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM universe_prefilter").fetchone()[0]
    assert before == after == 2


def test_a_run_cut_short_by_max_calls_is_incomplete_and_exits_ok(monkeypatch, tmp_path, clock):
    seed_db(tmp_path / "archive.db", survivor_ids=(1, 2, 3, 4))
    FakeSimFin(monkeypatch, statements={i: statements_record(i) for i in (1, 2, 3, 4)})
    code, output = do_run(tmp_path, clock, max_calls=1)
    assert code == rf.EXIT_OK
    assert "complete=False" in output and "not_fetched=2" in output


def test_resuming_after_a_budget_stop_fetches_only_the_still_missing_ids(monkeypatch, tmp_path, clock):
    seed_db(tmp_path / "archive.db", survivor_ids=(1, 2, 3, 4))
    fake = FakeSimFin(monkeypatch, statements={i: statements_record(i) for i in (1, 2, 3, 4)})
    do_run(tmp_path, clock, max_calls=1)
    before = len(fake.statements_requests())
    code, output = do_run(tmp_path, clock)
    assert code == rf.EXIT_OK and "complete=True" in output
    assert len(fake.statements_requests()) - before == 1  # only the missing batch [3, 4]


def test_a_failing_batch_is_isolated_and_retried_on_the_next_run(monkeypatch, tmp_path, clock):
    import urllib.error

    seed_db(tmp_path / "archive.db", survivor_ids=(1, 2, 3, 4))
    fake = FakeSimFin(monkeypatch, statements={i: statements_record(i) for i in (1, 2, 3, 4)})
    fake.fail_calls = {1: urllib.error.URLError("refused")}
    code, output = do_run(tmp_path, clock)
    assert code == rf.EXIT_OK and "complete=False" in output and "fetch_failed=2" in output

    fake.fail_calls = {}
    code, output = do_run(tmp_path, clock)
    assert code == rf.EXIT_OK and "complete=True" in output


# --- explicit full-batch silent-cap check -------------------------------------------


def test_the_summary_flags_a_full_batch_with_an_absent_id(monkeypatch, tmp_path, clock):
    seed_db(tmp_path / "archive.db", survivor_ids=(1, 2))
    FakeSimFin(monkeypatch, statements={1: statements_record(1)})  # id 2 absent from its full batch
    code, output = do_run(tmp_path, clock)
    assert code == rf.EXIT_OK
    assert "ACHTUNG: 1 von 1 vollen 2er-Batches" in output
    assert "batch=[1, 2] missing=[2]" in output


def test_the_summary_reports_a_clean_check_when_no_full_batch_has_a_gap(monkeypatch, tmp_path, clock):
    seed_db(tmp_path / "archive.db", survivor_ids=(1, 2))
    FakeSimFin(monkeypatch, statements={1: statements_record(1), 2: statements_record(2)})
    code, output = do_run(tmp_path, clock)
    assert code == rf.EXIT_OK
    assert "voller-Batch-Pruefung: 0 von 1 vollen 2er-Batches mit fehlender ID" in output


# --- abort after consecutive failures ---------------------------------------------


def test_an_abort_after_max_consecutive_failures_marks_the_rest_not_fetched_and_exits_ok(monkeypatch, tmp_path, clock):
    seed_db(tmp_path / "archive.db", survivor_ids=(1, 2, 3, 4))
    fake = FakeSimFin(monkeypatch, statements={i: statements_record(i) for i in (1, 2, 3, 4)})
    fake.fail_calls = {n: 429 for n in range(1, 20)}
    code, output = do_run(tmp_path, clock, max_consecutive_failures=2)
    assert code == rf.EXIT_OK
    assert "complete=False" in output and "abort_reason=consecutive_failures_abort" in output
    assert "not_fetched=4" in output


def test_the_default_limit_is_five_consecutive_failures(monkeypatch, tmp_path, clock):
    seed_db(tmp_path / "archive.db", survivor_ids=(1,))
    fake = FakeSimFin(monkeypatch, statements={1: statements_record(1)})
    fake.fail_calls = {n: 429 for n in range(1, 20)}
    code, output = do_run(tmp_path, clock)
    assert code == rf.EXIT_OK and "5 consecutive failures" in output


def test_an_invalid_max_consecutive_failures_is_a_technical_failure(monkeypatch, tmp_path, clock):
    seed_db(tmp_path / "archive.db", survivor_ids=(1,))
    FakeSimFin(monkeypatch, statements={1: statements_record(1)})
    code, output = do_run(tmp_path, clock, max_consecutive_failures=0)
    assert code == rf.EXIT_TECHNICAL_FAILURE


# --- technical failures ------------------------------------------------------------


def test_a_missing_api_key_is_a_technical_failure(tmp_path, clock, monkeypatch):
    seed_db(tmp_path / "archive.db", survivor_ids=(1,))
    monkeypatch.delenv("SIMFIN_API_KEY", raising=False)
    monkeypatch.setattr(rf, "resolve_api_key", lambda: (_ for _ in ()).throw(rf.Stage1RunError("no key")))
    code, output = do_run(tmp_path, clock, api_key=None)
    assert code == rf.EXIT_TECHNICAL_FAILURE


def test_no_run_timestamp_with_passed_survivors_is_a_technical_failure(tmp_path, clock):
    seed_db(tmp_path / "archive.db", survivor_ids=(), excluded_ids=(4,))
    code, output = do_run(tmp_path, clock)
    assert code == rf.EXIT_TECHNICAL_FAILURE


# --- run_timestamp / as_of resolution -----------------------------------------------


def test_an_explicit_run_timestamp_selects_that_runs_survivors_not_the_latest(monkeypatch, tmp_path, clock):
    db_path = tmp_path / "archive.db"
    seed_db(db_path, run_timestamp="2026-09-24T18:13:48", survivor_ids=(1,))
    seed_db(db_path, run_timestamp="2026-09-25T09:00:00", survivor_ids=(2,))
    FakeSimFin(monkeypatch, statements={1: statements_record(1), 2: statements_record(2)})
    code, output = do_run(tmp_path, clock, run_timestamp="2026-09-24T18:13:48")
    assert "run_timestamp=2026-09-24T18:13:48" in output and "survivors=1" in output


def test_the_default_as_of_is_the_survivors_run_timestamps_date_not_today(monkeypatch, tmp_path, clock):
    seed_db(tmp_path / "archive.db", run_timestamp="2026-09-24T18:13:48", survivor_ids=(1,))
    FakeSimFin(monkeypatch, statements={1: statements_record(1)})
    code, output = do_run(tmp_path, clock)
    assert "as_of=2026-09-24" in output
    assert (tmp_path / "cache" / "2026-09-24" / "1.json").exists()


def test_an_explicit_as_of_overrides_the_default(monkeypatch, tmp_path, clock):
    from datetime import date

    seed_db(tmp_path / "archive.db", run_timestamp="2026-09-24T18:13:48", survivor_ids=(1,))
    FakeSimFin(monkeypatch, statements={1: statements_record(1)})
    code, output = do_run(tmp_path, clock, as_of=date(2026, 1, 1))
    assert "as_of=2026-01-01" in output
    assert (tmp_path / "cache" / "2026-01-01" / "1.json").exists()


# --- CLI parser ----------------------------------------------------------------------


def test_the_parser_has_the_documented_flags_and_defaults():
    args = rf.build_parser().parse_args([])
    assert args.run_timestamp is None
    assert args.as_of is None
    assert args.max_calls is None
    assert args.max_consecutive_failures == rf.DEFAULT_MAX_CONSECUTIVE_FAILURES == 5
    assert args.db_path == rf.DEFAULT_DB_PATH
    assert args.cache_dir == rf.DEFAULT_CACHE_DIR


def test_main_passes_the_arguments_through_to_run(monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return rf.EXIT_OK

    monkeypatch.setattr(rf, "run", fake_run)
    rf.main(["--max-calls", "5", "--max-consecutive-failures", "2", "--run-timestamp", "2026-09-24T18:13:48"])
    assert captured["max_calls"] == 5
    assert captured["max_consecutive_failures"] == 2
    assert captured["run_timestamp"] == "2026-09-24T18:13:48"


def test_the_default_cache_dir_lives_under_the_gitignored_data_directory():
    assert rf.DEFAULT_CACHE_DIR.parts[-3:] == ("data", "cache", "fundamentals")

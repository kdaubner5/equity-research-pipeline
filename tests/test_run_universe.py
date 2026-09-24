"""End-to-end tests for run_universe.py (Issue #39): the whole chain (listing,
price batches, pre-filter, archive, survivors file) against a scripted fake
SimFin - offline and deterministic, no live call, no real waiting. All paths
point into tmp_path; the thresholds are a temp copy of the real config with a
small 5-row window."""

import io
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

import run_universe
from config_layer.thresholds import DEFAULT_THRESHOLDS_PATH, compute_config_hash
from gates_layer import universe_prefilter as pf
from run_stage1 import Stage1RunError
from universe_fakes import (  # noqa: F401  (clock is a fixture)
    AS_OF,
    FakeSimFin,
    clock,
    general_response,
    general_row,
    list_response,
    price_record,
)

RUN_TS = "2026-09-24T10:00:00"
PREFILTER = {
    "min_market_cap_usd": 100_000_000,
    "max_market_cap_usd": 1_000_000_000,
    "min_median_dollar_volume_usd": 500_000,
    "adv_window_trading_days": 5,
    "max_price_age_days": 3,
}
UNIVERSE_TXT = Path(__file__).resolve().parent.parent / "config" / "universe.txt"


def write_thresholds(tmp_path):
    data = yaml.safe_load(DEFAULT_THRESHOLDS_PATH.read_text(encoding="utf-8"))
    data["prefilter"] = dict(PREFILTER)
    path = tmp_path / "thresholds.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def good(cid, **overrides):
    """A price record that passes every threshold above, with overrides."""
    kwargs = dict(days=6, close=10.0, volume=1_000_000, shares=20_000_000)
    kwargs.update(overrides)
    return price_record(cid, **kwargs)


def scenario(monkeypatch):
    """One company per outcome; ids 6 (no prices), 8 (unclassified) and 9 (DE)
    get no price record. Returns (fake, expected reason by id)."""
    rows = [
        general_row(1, "PASS"),
        general_row(2, "SMALL"),
        general_row(3, "BIG"),
        general_row(4, "STALE"),
        general_row(5, "NOSH"),
        general_row(6, "GONE"),
        general_row(7, "THIN"),
        general_row(8, "NOCLS", sector=None),
        general_row(9, "DEUT", market="DE"),
        general_row(10, "SHORT"),
    ]
    records = {
        1: good(1),
        2: good(2, shares=1_000_000),  # cap 10 Mio.
        3: good(3, shares=500_000_000),  # cap 5 Mrd.
        4: good(4, last=AS_OF - timedelta(days=10)),
        5: good(5, shares=None),
        7: good(7, volume=1),
        10: good(10, days=2),
    }
    fake = FakeSimFin(monkeypatch, records=records, general=general_response(rows), companies_list=list_response(rows))
    expected = {
        1: None,
        2: pf.MARKET_CAP_BELOW_MIN,
        3: pf.MARKET_CAP_ABOVE_MAX,
        4: pf.PRICE_STALE,
        5: pf.NO_SHARES,
        6: pf.NO_PRICE_DATA,
        7: pf.MEDIAN_DOLLAR_VOLUME_BELOW_MIN,
        8: pf.NO_CLASSIFICATION,
        10: pf.INSUFFICIENT_PRICE_HISTORY,
    }
    return fake, expected


def do_run(tmp_path, clock, **overrides):
    out = io.StringIO()
    kwargs = dict(
        db_path=tmp_path / "archive.db",
        cache_dir=tmp_path / "cache",
        survivors_dir=tmp_path / "universe",
        api_key="test-key",
        as_of=AS_OF,
        run_timestamp=RUN_TS,
        thresholds_path=write_thresholds(tmp_path),
        sleep=clock.sleep,
        clock=clock,
        out=out,
    )
    kwargs.update(overrides)
    return run_universe.run(**kwargs), out.getvalue()


def read_archive(db_path, run_timestamp=RUN_TS):
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        return {
            row["simfin_id"]: dict(row)
            for row in connection.execute("SELECT * FROM universe_prefilter WHERE run_timestamp = ?", (run_timestamp,))
        }
    finally:
        connection.close()


def survivors_files(tmp_path):
    return sorted((tmp_path / "universe").glob("survivors_*.txt")) if (tmp_path / "universe").exists() else []


# --- the complete run ----------------------------------------------------------


def test_a_complete_run_archives_every_us_company_with_its_reason_and_lists_the_survivors(monkeypatch, tmp_path, clock):
    fake, expected = scenario(monkeypatch)
    code, output = do_run(tmp_path, clock)
    assert code == run_universe.EXIT_OK

    rows = read_archive(tmp_path / "archive.db")
    assert set(rows) == set(expected)  # all 9 US companies; the DE one (id 9) is counted, not archived
    assert {cid: row["reason"] for cid, row in rows.items()} == expected
    assert {cid: row["status"] for cid, row in rows.items()} == {
        cid: (pf.PASSED if reason is None else pf.EXCLUDED) for cid, reason in expected.items()
    }
    assert {row["config_hash"] for row in rows.values()} == {compute_config_hash(tmp_path / "thresholds.yaml")}

    # excluded companies keep their computed values (the archive's purpose)
    assert rows[2]["market_cap_usd"] == 10_000_000.0 and rows[2]["median_dollar_volume_usd"] == 10_000_000.0
    assert rows[4]["price_date"] == (AS_OF - timedelta(days=10)).isoformat() and rows[4]["market_cap_usd"] == 200_000_000.0
    assert rows[10]["median_window_rows"] == 2 and rows[6]["market_cap_usd"] is None
    assert rows[8]["market_cap_usd"] is None and rows[8]["sector_code"] is None

    (survivors,) = survivors_files(tmp_path)
    assert survivors.read_text(encoding="utf-8") == "PASS\n"

    # calls: 2 listings + 1 batch of the 8 classified candidates (the unclassified and the non-US company are
    # never asked for) + 1 single-id re-query of the one without prices
    endpoints = [endpoint for endpoint, _ in fake.requests]
    assert endpoints == ["general", "list", "prices", "prices"]
    assert fake.price_requests() == [[1, 2, 3, 4, 5, 6, 7, 10], [6]]  # then the single-id re-query of the empty one
    assert "complete=True" in output and "passed=1 excluded=8" in output
    assert "non_us=1" in output and "unclassified=1" in output and f"survivors_file={survivors}" in output
    assert "no_price_data=1" in output and "INCOMPLETE" not in output


def test_the_run_never_touches_config_universe_txt(monkeypatch, tmp_path, clock):
    before = UNIVERSE_TXT.read_bytes()
    scenario(monkeypatch)
    do_run(tmp_path, clock)
    assert UNIVERSE_TXT.read_bytes() == before


def test_a_complete_run_with_zero_survivors_still_writes_an_empty_file_and_exits_ok(monkeypatch, tmp_path, clock):
    rows = [general_row(1, "SMALL")]
    FakeSimFin(monkeypatch, records={1: good(1, shares=1_000_000)}, general=general_response(rows), companies_list=list_response(rows))
    code, output = do_run(tmp_path, clock)
    assert code == run_universe.EXIT_OK
    (survivors,) = survivors_files(tmp_path)
    assert survivors.read_bytes() == b""
    assert read_archive(tmp_path / "archive.db")[1]["reason"] == pf.MARKET_CAP_BELOW_MIN
    assert "passed=0 excluded=1" in output


# --- incomplete runs: budget and failed batches ---------------------------------


def test_a_run_cut_short_by_max_calls_is_archived_incomplete_without_a_survivors_file(monkeypatch, tmp_path, clock):
    fake, expected = scenario(monkeypatch)
    code, output = do_run(tmp_path, clock, max_calls=3)  # 2 listings + the batch; the re-query of id 6 no longer fits
    assert code == run_universe.EXIT_OK
    rows = read_archive(tmp_path / "archive.db")
    assert rows[6]["reason"] == pf.NOT_FETCHED  # never silently no_price_data
    assert {cid: row["reason"] for cid, row in rows.items() if cid != 6} == {c: r for c, r in expected.items() if c != 6}
    assert survivors_files(tmp_path) == []
    assert "complete=False" in output and "INCOMPLETE" in output and len(fake.requests) == 3


def test_a_budget_smaller_than_the_batches_marks_the_rest_not_fetched(monkeypatch, tmp_path, clock):
    rows = [general_row(i, f"T{i}") for i in range(1, 26)]
    fake = FakeSimFin(
        monkeypatch, records={i: good(i) for i in range(1, 26)}, general=general_response(rows), companies_list=list_response(rows)
    )
    code, _ = do_run(tmp_path, clock, max_calls=3)  # 2 listings + one batch of 10
    assert code == run_universe.EXIT_OK
    archived = read_archive(tmp_path / "archive.db")
    assert [archived[i]["reason"] for i in range(1, 11)] == [None] * 10
    assert [archived[i]["reason"] for i in range(11, 26)] == [pf.NOT_FETCHED] * 15
    assert len(fake.requests) == 3


def test_resuming_after_a_budget_stop_fetches_only_what_is_missing(monkeypatch, tmp_path, clock):
    fake, expected = scenario(monkeypatch)
    do_run(tmp_path, clock, max_calls=3)
    calls_before = len(fake.requests)

    code, output = do_run(tmp_path, clock, run_timestamp="2026-09-24T11:00:00")  # same as_of -> same cache
    assert code == run_universe.EXIT_OK
    assert len(fake.requests) - calls_before == 1  # listings and the batch came from the cache; only the re-query is new
    assert fake.requests[-1] == ("prices", [6])
    second = read_archive(tmp_path / "archive.db", "2026-09-24T11:00:00")
    assert {cid: row["reason"] for cid, row in second.items()} == expected
    assert len(survivors_files(tmp_path)) == 1 and "complete=True" in output
    assert len(read_archive(tmp_path / "archive.db")) == len(expected)  # the first, incomplete run is still archived


def test_a_failing_batch_is_isolated_archived_as_price_fetch_failed_and_retried_on_the_next_run(monkeypatch, tmp_path, clock):
    fake, expected = scenario(monkeypatch)
    fake.fail_calls = {3: 500}  # the price batch
    code, output = do_run(tmp_path, clock)
    assert code == run_universe.EXIT_OK
    rows = read_archive(tmp_path / "archive.db")
    assert {cid: row["reason"] for cid, row in rows.items() if cid in (1, 2, 3, 4, 5, 6, 7, 10)} == {
        cid: pf.PRICE_FETCH_FAILED for cid in (1, 2, 3, 4, 5, 6, 7, 10)
    }
    assert rows[8]["reason"] == pf.NO_CLASSIFICATION  # unaffected by the failed batch
    assert survivors_files(tmp_path) == [] and "INCOMPLETE" in output and "error (batch 1..10)" in output

    fake.fail_calls = {}
    before = len(fake.requests)
    code, _ = do_run(tmp_path, clock, run_timestamp="2026-09-24T11:00:00")
    assert code == run_universe.EXIT_OK
    assert len(fake.requests) - before == 2  # the batch and the re-query, nothing else
    assert {cid: row["reason"] for cid, row in read_archive(tmp_path / "archive.db", "2026-09-24T11:00:00").items()} == expected


# --- ordering ---------------------------------------------------------------------


def test_priority_tickers_are_fetched_first_and_an_unknown_one_is_reported(monkeypatch, tmp_path, clock):
    rows = [general_row(i, f"T{i}") for i in range(1, 26)]
    fake = FakeSimFin(
        monkeypatch, records={i: good(i) for i in range(1, 26)}, general=general_response(rows), companies_list=list_response(rows)
    )
    code, output = do_run(tmp_path, clock, priority_tickers=["t25", "NOPE", "T24"])
    assert code == run_universe.EXIT_OK
    batches = fake.price_requests()
    assert batches[0][:3] == [25, 24, 1] and sorted(sum(batches, [])) == list(range(1, 26))
    assert "priority tickers not found" in output and "NOPE" in output


# --- technical failures: exit 1, nothing written -----------------------------------


def _nothing_written(tmp_path):
    return not (tmp_path / "archive.db").exists() and survivors_files(tmp_path) == []


def test_a_missing_api_key_is_a_technical_failure(monkeypatch, tmp_path, clock, capsys):
    def no_key():
        raise Stage1RunError("SIMFIN_API_KEY not set (checked the environment and .env)")

    monkeypatch.setattr(run_universe, "resolve_api_key", no_key)
    scenario(monkeypatch)
    code, _ = do_run(tmp_path, clock, api_key=None)
    assert code == run_universe.EXIT_TECHNICAL_FAILURE
    assert "SIMFIN_API_KEY" in capsys.readouterr().err and _nothing_written(tmp_path)


def test_an_invalid_thresholds_file_is_a_technical_failure(monkeypatch, tmp_path, clock, capsys):
    fake, _ = scenario(monkeypatch)
    bad = tmp_path / "bad.yaml"
    bad.write_text("roic:\n  fail_below: 0.1\n", encoding="utf-8")
    code, _ = do_run(tmp_path, clock, thresholds_path=bad)
    assert code == run_universe.EXIT_TECHNICAL_FAILURE
    assert "thresholds.yaml invalid" in capsys.readouterr().err and fake.requests == [] and _nothing_written(tmp_path)


def test_an_unreachable_listing_is_a_technical_failure(monkeypatch, tmp_path, clock, capsys):
    fake, _ = scenario(monkeypatch)
    fake.fail_calls = {1: 500}
    code, _ = do_run(tmp_path, clock)
    assert code == run_universe.EXIT_TECHNICAL_FAILURE
    assert "universe listing unavailable" in capsys.readouterr().err and _nothing_written(tmp_path)


def test_an_unusable_listing_shape_is_a_technical_failure(monkeypatch, tmp_path, clock, capsys):
    FakeSimFin(monkeypatch, general=[], companies_list=[])  # general/compact must be {"columns", "data"}
    code, _ = do_run(tmp_path, clock)
    assert code == run_universe.EXIT_TECHNICAL_FAILURE
    assert "universe listing unavailable" in capsys.readouterr().err and _nothing_written(tmp_path)


def test_a_budget_too_small_to_load_the_listing_is_a_technical_failure(monkeypatch, tmp_path, clock, capsys):
    scenario(monkeypatch)
    code, _ = do_run(tmp_path, clock, max_calls=1)
    assert code == run_universe.EXIT_TECHNICAL_FAILURE
    assert "universe listing unavailable" in capsys.readouterr().err and _nothing_written(tmp_path)


def test_duplicate_tickers_among_the_candidates_are_a_technical_failure_before_any_price_call(monkeypatch, tmp_path, clock, capsys):
    rows = [general_row(1, "SAME"), general_row(2, "same")]
    fake = FakeSimFin(monkeypatch, records={}, general=general_response(rows), companies_list=list_response(rows))
    code, _ = do_run(tmp_path, clock)
    assert code == run_universe.EXIT_TECHNICAL_FAILURE
    assert "duplicate ticker" in capsys.readouterr().err
    assert fake.price_requests() == [] and _nothing_written(tmp_path)


def test_a_failing_archive_write_removes_the_survivors_file_it_already_wrote(monkeypatch, tmp_path, clock, capsys):
    scenario(monkeypatch)
    db_dir = tmp_path / "db_is_a_directory"
    db_dir.mkdir()  # sqlite cannot open a directory as a database
    code, _ = do_run(tmp_path, clock, db_path=db_dir)
    assert code == run_universe.EXIT_TECHNICAL_FAILURE
    assert "could not write run results" in capsys.readouterr().err
    assert survivors_files(tmp_path) == [] and not list((tmp_path / "universe").glob("*.tmp"))


# --- command line --------------------------------------------------------------------


def test_the_parser_reads_max_calls_and_as_of_and_rejects_a_negative_budget():
    args = run_universe.build_parser().parse_args(["--max-calls", "150", "--as-of", "2026-09-24"])
    assert args.max_calls == 150 and args.as_of == AS_OF
    defaults = run_universe.build_parser().parse_args([])
    assert defaults.max_calls is None and defaults.as_of is None and defaults.priority_tickers_file is None
    with pytest.raises(SystemExit):
        run_universe.build_parser().parse_args(["--max-calls", "-1"])
    with pytest.raises(SystemExit):
        run_universe.build_parser().parse_args(["--as-of", "yesterday"])


def test_main_passes_the_arguments_through_to_run(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(run_universe, "run", lambda **kwargs: captured.update(kwargs) or 0)
    priority = tmp_path / "priority.txt"
    priority.write_text("AAA\n\n  bbb  \n", encoding="utf-8")
    code = run_universe.main(
        ["--max-calls", "7", "--as-of", "2026-09-24", "--db-path", str(tmp_path / "x.db"), "--priority-tickers-file", str(priority)]
    )
    assert code == 0
    assert captured["max_calls"] == 7 and captured["as_of"] == AS_OF and captured["db_path"] == tmp_path / "x.db"
    assert captured["priority_tickers"] == ["AAA", "bbb"]
    assert captured["survivors_dir"] == run_universe.DEFAULT_UNIVERSE_DIR  # defaults come from the store layer/cache dir constants


def test_the_default_paths_live_under_the_gitignored_data_directory():
    root = Path(run_universe.__file__).resolve().parent
    for path in (run_universe.DEFAULT_CACHE_DIR, run_universe.DEFAULT_DB_PATH, run_universe.DEFAULT_UNIVERSE_DIR):
        assert path.relative_to(root).parts[0] == "data"
    assert "data/" in (root / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_load_priority_tickers_skips_blank_lines(tmp_path):
    path = tmp_path / "p.txt"
    path.write_text("\nAAA\n  \nBBB\n", encoding="utf-8")
    assert run_universe.load_priority_tickers(path) == ["AAA", "BBB"]

"""End-to-end tests for run_stage2.py (docs/decisions.md "Stufe 2 - Beantwortung Q1-Q9 und
Runner", Entscheidung R1): offline, deterministic, no network.

Test data: the 23 committed pilot raw-SimFin fixtures as the fundamentals cache, artificial
prefilter rows and artificial Stage-1 survivors, all in tmp_path. Every run() call passes
tmp_path-based paths explicitly - this file never touches data/ or the real archive.

Facts about the fixtures at as_of 2026-09-24 with a market cap of 1e9 each (measured with the
Stage-2 metrics, then pinned here): nine pilots have a valid EV/EBITDA - CENT 3.91, SCSC 8.233,
DXPE 8.237, SMP 8.47, UFPT 9.95, USNA 10.26, PLOW 12.8, FLXS 27.5, CVGI 76.2; KELYA has no
periods at all.
"""

import copy
import hashlib
import io
import json
import sqlite3
from pathlib import Path

import pytest

from data_layer import simfin_fundamentals as sf
from gates_layer.stage1_gates import PASSED, PASSED_WITH_FLAGS, GateResult
from gates_layer.universe_prefilter import PASSED as PREFILTER_PASSED
from gates_layer.universe_prefilter import PrefilterResult
from store_layer.db import init_db, save_run_results, save_universe_prefilter

import run_stage2

SIMFIN_RAW_DIR = Path(__file__).parent / "fixtures" / "simfin_raw"
PREFILTER_TS = "2026-09-24T18:13:48"
STAGE1_TS = "2026-09-30T17:38:53"
RUN_TS = "2026-10-06T12:00:00"
SECTOR = 101003  # a non_financial code in the real config/sector_profiles.yaml
BANK_SECTOR = 104002  # an unsupported profile (banks)
ALL_PILOTS = sorted(p.stem for p in SIMFIN_RAW_DIR.glob("*.json"))
VALID_EV_PILOTS = {"CENT", "SCSC", "DXPE", "SMP", "UFPT", "USNA", "PLOW", "FLXS", "CVGI"}


def raw(ticker):
    with open(SIMFIN_RAW_DIR / f"{ticker}.json", encoding="utf-8") as f:
        return json.load(f)


def prefilter_row(simfin_id, ticker, *, sector=SECTOR, cap=1_000_000_000):
    return PrefilterResult(
        simfin_id=simfin_id, ticker=ticker, sector_code=sector, status=PREFILTER_PASSED, reason=None,
        market_cap_usd=cap, median_dollar_volume_usd=2_000_000, median_window_rows=60, price_date=None,
    )


def s1(status=PASSED, flags=()):
    return GateResult(
        status=status, flags=tuple(flags), invalid_reasons=(), roic=0.2, net_debt_ebitda=1.0,
        cash_conversion=1.1, net_income_margin=0.1,
    )


def seed_cache(cache_dir, simfin_id, response, *, outcome=sf.FETCH_OK):
    sf._write_envelope(
        sf.cache_path(cache_dir, simfin_id),
        {
            "kind": "fundamentals_raw", "simfin_id": simfin_id, "statements": list(sf.DEFAULT_STATEMENTS),
            "outcome": outcome, "fetched_at_utc": "x", "rate_limit_remaining": None, "response": response,
        },
    )


class World:
    """tmp_path-based archive, cache and output directory, seeded with the pilots."""

    def __init__(self, tmp_path, *, survivors=("CENT", "SCSC", "KELYA"), extra_prefilter=(), pilots=ALL_PILOTS):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.source_db = tmp_path / "source.db"
        self.archive_db = tmp_path / "archive.db"
        self.csv_dir = tmp_path / "csv"
        self.cache_root = tmp_path / "cache"
        self.cache_dir = self.cache_root / "2026-09-24"
        rows = []
        for ticker in pilots:
            record = raw(ticker)
            seed_cache(self.cache_dir, record["id"], record)
            rows.append(prefilter_row(record["id"], ticker))
        rows.extend(extra_prefilter)
        init_db(self.source_db)
        save_universe_prefilter(PREFILTER_TS, rows, config_hash="deadbeef", db_path=self.source_db, rule_state=None)
        stage1 = {t: s1(PASSED_WITH_FLAGS, ("roic",)) if t == "SCSC" else s1() for t in survivors}
        save_run_results(STAGE1_TS, stage1, config_hash="c", db_path=self.source_db, rule_state=None)

    def run(self, **overrides):
        kwargs = dict(
            db_path=self.source_db, archive_db_path=self.archive_db, csv_dir=self.csv_dir,
            fundamentals_cache_dir=self.cache_root, stage1_run_timestamp=STAGE1_TS, run_timestamp=RUN_TS,
            out=io.StringIO(),
        )
        kwargs.update(overrides)
        out = kwargs["out"]
        code = run_stage2.run(**kwargs)
        return code, out.getvalue()

    def stage2(self, sql):
        conn = sqlite3.connect(self.archive_db)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def nothing_written(world):
    return not world.archive_db.exists() and not world.csv_dir.exists()


# ---------------------------------------------------------------- happy path


def test_full_run_on_the_pilot_fixtures(tmp_path):
    world = World(tmp_path)
    code, out = world.run()
    assert code == run_stage2.EXIT_OK, out

    run_row = world.stage2(
        "SELECT stage1_run_timestamp, prefilter_run_timestamp, as_of, companies_evaluated, ranked_count, peer_base_size "
        "FROM stage2_runs"
    )
    # 3 Stage-1 survivors; CENT and SCSC pass Stage 2, KELYA has no data; peer base = the 9 valid multiples
    assert run_row == [(STAGE1_TS, PREFILTER_TS, "2026-09-24", 3, 2, 9)]

    got = {r[0]: r for r in world.stage2(
        "SELECT ticker, status, flags, invalid_reasons, peer_level, peer_group, peer_rank, peer_n, rank_position, simfin_id "
        "FROM stage2_results"
    )}
    assert set(got) == {"CENT", "SCSC", "KELYA"}  # exactly the Stage-1 survivors
    assert got["CENT"][1:9] == ("passed", "", "", "fine", SECTOR, 1, 9, 1)
    assert got["SCSC"][1:9] == ("passed", "", "", "fine", SECTOR, 2, 9, 2)  # one Stage-1 flag ranks it after CENT
    assert got["KELYA"][1] == "not_evaluable" and got["KELYA"][3] == "no_periods,no_periods"
    assert got["KELYA"][8] is None
    assert got["CENT"][9] == raw("CENT")["id"]

    csv_files = list(world.csv_dir.glob("stage2_survivors_*.csv"))
    assert len(csv_files) == 1
    lines = csv_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert [line.split(",")[1] for line in lines[1:]] == ["CENT", "SCSC"]


def test_summary_names_the_counts_with_their_reference(tmp_path):
    world = World(tmp_path)
    _, out = world.run()
    assert "prefilter_run_timestamp=" + PREFILTER_TS in out
    assert "peer_base=9" in out
    assert "survivors_evaluated=3" in out
    assert "passed=2" in out and "not_evaluable=1" in out
    assert "no_periods" in out  # the reasons are listed, not hidden


def test_the_source_archive_is_not_modified_when_results_go_elsewhere(tmp_path):
    world = World(tmp_path)
    before = sha(world.source_db)
    code, _ = world.run()
    assert code == 0
    assert sha(world.source_db) == before


def test_results_do_not_depend_on_the_order_of_the_survivors(tmp_path):
    first = World(tmp_path / "a", survivors=("CENT", "SCSC", "UFPT"))
    second = World(tmp_path / "b", survivors=("UFPT", "SCSC", "CENT"))
    first.run()
    second.run()
    sql = "SELECT ticker, status, ev_ebitda, ev_ebitda_percentile, rank_position FROM stage2_results ORDER BY ticker"
    assert first.stage2(sql) == second.stage2(sql)


# ---------------------------------------------------------------- explicit handling of unusable inputs


def test_survivors_with_unusable_fundamentals_get_an_explicit_reason_each(tmp_path):
    base = raw("UFPT")
    missing = dict(copy.deepcopy(base), id=9001, ticker="MISS")
    corrupt = dict(copy.deepcopy(base), id=9002, ticker="CORR")
    absent = dict(copy.deepcopy(base), id=9003, ticker="ABSE")
    broken = copy.deepcopy(base)
    broken.update(id=9004, ticker="MAPE")
    for stmt in broken["statements"]:
        if stmt["statement"] == "PL":
            stmt["columns"] = ["Rev" if c == "Revenue" else c for c in stmt["columns"]]
    extra = [prefilter_row(r["id"], r["ticker"]) for r in (missing, corrupt, absent, broken)]
    world = World(tmp_path, survivors=("MISS", "CORR", "ABSE", "MAPE", "UFPT"), extra_prefilter=extra)
    seed_cache(world.cache_dir, 9003, None, outcome=sf.FETCH_ABSENT)
    seed_cache(world.cache_dir, 9004, broken)
    sf.cache_path(world.cache_dir, 9002).parent.mkdir(parents=True, exist_ok=True)
    sf.cache_path(world.cache_dir, 9002).write_text("{not json", encoding="utf-8")
    # MISS (9001): no cache file at all

    code, out = world.run()
    assert code == 0, out
    reasons = {r[0]: (r[1], r[2]) for r in world.stage2("SELECT ticker, status, invalid_reasons FROM stage2_results")}
    assert reasons["MISS"] == ("not_evaluable", "fundamentals_cache_missing")
    assert reasons["ABSE"] == ("not_evaluable", "fundamentals_cache_missing")
    assert reasons["CORR"] == ("not_evaluable", "fundamentals_cache_invalid")
    assert reasons["MAPE"] == ("not_evaluable", "simfin_mapping_error")
    assert reasons["UFPT"][0] != "not_evaluable"
    assert "fundamentals_cache_invalid" in out and "simfin_mapping_error" in out


def test_a_company_with_unusable_fundamentals_is_just_not_a_peer(tmp_path):
    world = World(tmp_path, extra_prefilter=[prefilter_row(9100, "GHOST")])  # no cache file for 9100
    code, _ = world.run()
    assert code == 0
    assert world.stage2("SELECT peer_base_size FROM stage2_runs") == [(9,)]


def test_a_missing_capex_column_makes_the_yield_invalid_but_keeps_the_peer(tmp_path):
    world = World(tmp_path)
    record = raw("CENT")
    cf = next(s for s in record["statements"] if s["statement"] == "CF")
    idx = cf["columns"].index("Change in Fixed Assets & Intangibles")
    cf["columns"].pop(idx)
    for row in cf["data"]:
        row.pop(idx)
    seed_cache(world.cache_dir, record["id"], record)
    code, out = world.run()
    assert code == 0, out
    row = world.stage2("SELECT status, invalid_reasons, ev_ebitda FROM stage2_results WHERE ticker = 'CENT'")[0]
    assert row[0] == "not_evaluable" and row[1] == "capex_missing" and row[2] == pytest.approx(3.9088512)
    assert world.stage2("SELECT peer_base_size FROM stage2_runs") == [(9,)]  # EV does not need capex
    assert "capex_unavailable=1" in out


def test_a_survivor_missing_from_the_prefilter_is_reported_not_skipped(tmp_path):
    world = World(tmp_path, survivors=("CENT", "NOTINUNIVERSE"))
    code, out = world.run()
    assert code == 0
    assert world.stage2("SELECT status, invalid_reasons, simfin_id FROM stage2_results WHERE ticker = 'NOTINUNIVERSE'") == [
        ("not_evaluable", "valuation_input_missing", None)
    ]
    assert "valuation_input_missing" in out


def test_an_unsupported_profile_company_is_not_a_peer_and_reads_no_cache(tmp_path):
    bank = prefilter_row(9200, "BANKX", sector=BANK_SECTOR)  # no cache file: must not matter
    world = World(tmp_path, extra_prefilter=[bank])
    code, out = world.run()
    assert code == 0, out
    assert world.stage2("SELECT peer_base_size FROM stage2_runs") == [(9,)]
    assert "profile_unsupported_rows=1" in out


def test_a_prefilter_row_without_ticker_is_reported_and_not_evaluated(tmp_path):
    nameless = PrefilterResult(
        simfin_id=9300, ticker=None, sector_code=SECTOR, status=PREFILTER_PASSED, reason=None,
        market_cap_usd=1e9, median_dollar_volume_usd=2e6, median_window_rows=60, price_date=None,
    )
    world = World(tmp_path, extra_prefilter=[nameless])
    code, out = world.run()
    assert code == 0, out
    assert "ohne Ticker" in out and "9300" in out


def test_survivors_in_different_sectors_use_the_fallback_levels_and_mark_them(tmp_path):
    # SMP alone in sector 105001: 1 valid value in its fine and coarse group -> absolute level
    world = World(tmp_path, survivors=("SMP",))
    conn = sqlite3.connect(world.source_db)
    conn.execute("UPDATE universe_prefilter SET sector_code = 105001 WHERE ticker = 'SMP'")
    conn.commit()
    conn.close()
    code, out = world.run()
    assert code == 0, out
    row = world.stage2("SELECT peer_level, peer_n_fine, peer_n_coarse, info_flags FROM stage2_results")[0]
    assert row[0] == "absolute" and row[1] == 1 and row[2] == 1 and "peer_group_absolute" in row[3]
    assert world.stage2("SELECT peer_base_size FROM stage2_runs") == [(9,)]


# ---------------------------------------------------------------- technical failures write nothing


def test_duplicate_tickers_abort_before_anything_is_written(tmp_path):
    world = World(tmp_path, extra_prefilter=[prefilter_row(9400, "CENT")])
    code, _ = world.run()
    assert code == run_stage2.EXIT_TECHNICAL_FAILURE
    assert nothing_written(world)


def test_a_sector_code_missing_from_the_profile_config_aborts(tmp_path):
    world = World(tmp_path, extra_prefilter=[prefilter_row(9500, "WEIRD", sector=999999)])
    code, _ = world.run()
    assert code == run_stage2.EXIT_TECHNICAL_FAILURE
    assert nothing_written(world)


def test_an_unknown_stage1_run_aborts(tmp_path):
    world = World(tmp_path)
    code, _ = world.run(stage1_run_timestamp="1999-01-01T00:00:00")
    assert code == run_stage2.EXIT_TECHNICAL_FAILURE
    assert nothing_written(world)


def test_a_missing_source_archive_aborts_and_creates_nothing(tmp_path):
    world = World(tmp_path)
    code, _ = world.run(db_path=tmp_path / "does_not_exist.db")
    assert code == run_stage2.EXIT_TECHNICAL_FAILURE
    assert not (tmp_path / "does_not_exist.db").exists()  # opened read-only: never created
    assert nothing_written(world)


def test_an_unknown_prefilter_run_aborts(tmp_path):
    world = World(tmp_path)
    code, _ = world.run(prefilter_run_timestamp="1999-01-01T00:00:00")
    assert code == run_stage2.EXIT_TECHNICAL_FAILURE
    assert nothing_written(world)


def test_an_invalid_stage2_config_aborts(tmp_path):
    world = World(tmp_path)
    bad = tmp_path / "bad.yaml"
    bad.write_text("peer_groups: {}\n", encoding="utf-8")
    code, out = world.run(stage2_config_path=bad)
    assert code == run_stage2.EXIT_TECHNICAL_FAILURE
    assert nothing_written(world)


def test_a_failing_archive_write_removes_the_csv_and_reports_failure(tmp_path, monkeypatch):
    world = World(tmp_path)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(run_stage2, "save_stage2_results", boom)
    code, _ = world.run()
    assert code == run_stage2.EXIT_TECHNICAL_FAILURE
    assert not list(world.csv_dir.glob("*.csv"))


def test_running_twice_with_the_same_run_timestamp_is_refused(tmp_path):
    world = World(tmp_path)
    assert world.run()[0] == 0
    code, _ = world.run()
    assert code == run_stage2.EXIT_TECHNICAL_FAILURE
    assert world.stage2("SELECT COUNT(*) FROM stage2_runs") == [(1,)]


# ---------------------------------------------------------------- command line


def test_no_path_has_a_default_so_the_real_archive_is_never_touched_by_accident():
    parser = run_stage2.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    required = {a.dest for a in parser._actions if a.required}
    assert {"db_path", "archive_db_path", "csv_dir", "fundamentals_cache_dir", "stage1_run_timestamp"} <= required


def test_main_runs_end_to_end_from_the_command_line(tmp_path, capsys):
    world = World(tmp_path)
    argv = [
        "--db-path", str(world.source_db), "--archive-db-path", str(world.archive_db), "--csv-dir", str(world.csv_dir),
        "--fundamentals-cache-dir", str(world.cache_root), "--stage1-run-timestamp", STAGE1_TS,
    ]
    assert run_stage2.main(argv) == 0
    assert world.stage2("SELECT COUNT(*) FROM stage2_runs") == [(1,)]
    assert "peer_base=9" in capsys.readouterr().out


# ---------------------------------------------------------------- one malformed company must not abort the run (critic finding)


def _malformed_world(tmp_path, mutate):
    world = World(tmp_path, survivors=("CENT", "SCSC"))
    record = raw("CENT")
    mutate(record)
    seed_cache(world.cache_dir, record["id"], record)
    return world


def _cf(record):
    return next(s for s in record["statements"] if s["statement"] == "CF")


def _capex_to_string(record):
    cf = _cf(record)
    idx = cf["columns"].index("Change in Fixed Assets & Intangibles")
    for row in cf["data"]:
        if row[idx] is not None:
            row[idx] = "n/a"


def _short_capex_rows(record):
    cf = _cf(record)
    idx = cf["columns"].index("Change in Fixed Assets & Intangibles")
    for row in cf["data"]:
        del row[idx:]  # shorter than the column list: the capex cell is missing


def _capex_to_bool(record):
    cf = _cf(record)
    idx = cf["columns"].index("Change in Fixed Assets & Intangibles")
    for row in cf["data"]:
        row[idx] = True


def _ebitda_part_to_string(record):
    cf = _cf(record)
    idx = cf["columns"].index("Depreciation & Amortization")
    for row in cf["data"]:
        row[idx] = "x"


@pytest.mark.parametrize("mutate", [_capex_to_string, _short_capex_rows, _capex_to_bool, _ebitda_part_to_string])
def test_a_malformed_company_gets_an_explicit_reason_and_the_run_goes_on(tmp_path, mutate):
    world = _malformed_world(tmp_path, mutate)
    code, out = world.run()
    assert code == run_stage2.EXIT_OK, out
    rows = {r[0]: r[1:] for r in world.stage2("SELECT ticker, status, invalid_reasons FROM stage2_results")}
    assert rows["CENT"] == ("not_evaluable", "fundamentals_malformed")
    assert rows["SCSC"][0] == "passed"  # the other survivor is unaffected
    assert "fundamentals_malformed" in out
    # a malformed company is not a peer either: 9 valid multiples minus CENT
    assert world.stage2("SELECT peer_base_size FROM stage2_runs") == [(8,)]


def test_summary_counts_companies_not_reasons(tmp_path):
    world = World(tmp_path)  # KELYA: no_periods for both metrics, but it is ONE company
    _, out = world.run()
    line = next(l for l in out.splitlines() if l.startswith("invalid reasons"))
    assert "no_periods=1" in line and "von 3" in line


def test_the_summary_names_the_cache_directory_that_was_read(tmp_path):
    world = World(tmp_path)
    _, out = world.run()
    assert str(world.cache_dir) in out


# ---------------------------------------------------------------- second critic run


def _set_cell(record, statement, column, value):
    stmt = next(s for s in record["statements"] if s["statement"] == statement)
    idx = stmt["columns"].index(column)
    for row in stmt["data"]:
        row[idx] = value


@pytest.mark.parametrize(
    "statement,column",
    [
        ("PL", "Operating Income (Loss)"),
        ("CF", "Depreciation & Amortization"),
        ("CF", "Cash from Operating Activities"),
        ("BS", "Cash & Cash Equivalents"),
        ("BS", "Short Term Debt"),
        ("CF", "Change in Fixed Assets & Intangibles"),
    ],
)
def test_an_integer_too_large_for_a_float_does_not_abort_the_run(tmp_path, statement, column):
    world = _malformed_world(tmp_path, lambda record: _set_cell(record, statement, column, 10**400))
    code, out = world.run()
    assert code == run_stage2.EXIT_OK, out
    rows = {r[0]: r[1] for r in world.stage2("SELECT ticker, status FROM stage2_results")}
    assert rows["CENT"] == "not_evaluable"
    assert rows["SCSC"] == "passed"


def test_a_null_sector_code_aborts_before_anything_is_written(tmp_path):
    world = World(tmp_path)
    conn = sqlite3.connect(world.source_db)
    conn.execute("UPDATE universe_prefilter SET sector_code = NULL WHERE ticker = 'CENT'")
    conn.commit()
    conn.close()
    code, _ = world.run()
    assert code == run_stage2.EXIT_TECHNICAL_FAILURE
    assert nothing_written(world)


def test_an_unreadable_prefilter_timestamp_aborts_instead_of_a_traceback(tmp_path):
    world = World(tmp_path)
    conn = sqlite3.connect(world.source_db)
    conn.execute("UPDATE universe_prefilter SET run_timestamp = 'garbage-timestamp'")
    conn.commit()
    conn.close()
    code, _ = world.run(prefilter_run_timestamp="garbage-timestamp")
    assert code == run_stage2.EXIT_TECHNICAL_FAILURE
    assert nothing_written(world)


def test_a_repeated_run_leaves_the_first_csv_untouched(tmp_path):
    world = World(tmp_path)
    assert world.run()[0] == 0
    (first,) = world.csv_dir.glob("stage2_survivors_*.csv")
    before = first.read_bytes()
    assert world.run()[0] == run_stage2.EXIT_TECHNICAL_FAILURE
    assert first.read_bytes() == before


def test_a_failing_archive_write_does_not_leave_result_rows_behind(tmp_path, monkeypatch):
    world = World(tmp_path)
    monkeypatch.setattr(run_stage2, "save_stage2_results", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
    assert world.run()[0] == run_stage2.EXIT_TECHNICAL_FAILURE
    assert not world.archive_db.exists()  # nothing was even created: the failure came before any write


def test_a_market_cap_of_the_wrong_type_is_isolated_not_a_traceback(tmp_path):
    world = World(tmp_path, survivors=("CENT", "SCSC"))
    conn = sqlite3.connect(world.source_db)
    conn.execute("UPDATE universe_prefilter SET market_cap_usd = 'abc' WHERE ticker = 'CENT'")
    conn.commit()
    conn.close()
    code, out = world.run()
    assert code == run_stage2.EXIT_OK, out
    rows = {r[0]: r[1:] for r in world.stage2("SELECT ticker, status, invalid_reasons FROM stage2_results")}
    assert rows["CENT"] == ("not_evaluable", "market_cap_malformed")
    assert rows["SCSC"][0] == "passed"
    assert world.stage2("SELECT peer_base_size FROM stage2_runs") == [(8,)]


# ---------------------------------------------------------------- rule state (docs/decisions.md "Regelstand und Kurs je Lauf")


def _stage2_argv(world):
    return [
        "--db-path", str(world.source_db), "--archive-db-path", str(world.archive_db), "--csv-dir", str(world.csv_dir),
        "--fundamentals-cache-dir", str(world.cache_root), "--stage1-run-timestamp", STAGE1_TS,
    ]


def test_the_run_archives_its_rule_state_with_the_four_rule_files(tmp_path):
    from store_layer.rule_state import RULE_FILES

    world = World(tmp_path)
    assert run_stage2.main(_stage2_argv(world)) == 0
    assert [r[0] for r in world.stage2("SELECT step FROM run_rule_state")] == ["stage2"]
    assert {r[0] for r in world.stage2("SELECT path FROM run_rule_files")} == set(RULE_FILES)


def test_a_missing_rule_file_stops_stage2_and_writes_nothing(monkeypatch, tmp_path):
    from store_layer.rule_state import RuleStateError

    def broken(**kwargs):
        raise RuleStateError("rule file config/trend_guard.yaml not readable")

    monkeypatch.setattr(run_stage2, "capture_rule_state", broken)
    world = World(tmp_path)
    assert run_stage2.main(_stage2_argv(world)) == 1
    assert nothing_written(world)

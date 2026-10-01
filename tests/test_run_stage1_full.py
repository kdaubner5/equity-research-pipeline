"""End-to-end tests for run_stage1_full.py (Issue #45, docs/decisions.md
"Phase 2.0c"): the fundamentals-cache path, offline and deterministic - no
urlopen mock is ever installed in this file, since evaluate_from_cache()/
run() never touch the network (docs/decisions.md "Phase 2.0c" Entscheidung
3: a cached envelope's response is fed straight to run_stage1.evaluate_ticker,
which is itself network-free).

Every rsf.run() call in this file passes tmp_path-based db_path/
archive_db_path/csv_dir/fundamentals_cache_dir explicitly - never the
module defaults, which point at the real data/run_archive.db and the real
fundamentals cache. This file never touches those real paths.
"""

import io
import json
import sqlite3
from pathlib import Path

import pytest

from conftest import DEFAULT_THRESHOLDS
from config_layer.sector_profiles import DEFAULT_SECTOR_PROFILES_PATH, load_sector_profiles
from data_layer import simfin_fundamentals as sf
from gates_layer.universe_prefilter import EXCLUDED, PASSED, PrefilterResult
from store_layer.db import save_universe_prefilter

import run_stage1
import run_stage1_full as rsf

SIMFIN_RAW_DIR = Path(__file__).parent / "fixtures" / "simfin_raw"

# The 16 of 23 pilots (config/universe.txt) that passed the universe
# pre-filter and are therefore in the fundamentals cache (docs/decisions.md
# "Phase 2.0c" Entscheidung 5); the other 7 (CATO, CULP, CVGI, GENC, LAKE,
# SGC, USNA) are out of scope for this stage's regression.
PILOTS_IN_CACHE = [
    "ATRO", "CENT", "DXPE", "FLXS", "FOSL", "KELYA", "LZB", "MATW", "MOV",
    "NPK", "PLOW", "ROCK", "SCSC", "SMP", "SUPN", "UFPT",
]

RUN_TS = "2026-09-24T18:13:48"  # date portion 2026-09-24


def _load_raw(ticker):
    with open(SIMFIN_RAW_DIR / f"{ticker}.json", encoding="utf-8") as f:
        return json.load(f)


# The sector code every seeded survivor carries. run() checks it against the real
# config/sector_profiles.yaml (docs/decisions.md "Phase 2.0e", Entscheidung 2), so it
# must be a code listed there under the standard profile - see
# test_the_seeded_survivor_sector_code_is_a_standard_profile_code_in_the_real_config.
SEEDED_SECTOR_CODE = 101003


def passed(simfin_id, ticker):
    return PrefilterResult(
        simfin_id=simfin_id,
        ticker=ticker,
        sector_code=SEEDED_SECTOR_CODE,
        status=PASSED,
        reason=None,
        market_cap_usd=1_000_000_000,
        median_dollar_volume_usd=2_000_000,
        median_window_rows=60,
        price_date=None,
    )


def test_the_seeded_survivor_sector_code_is_a_standard_profile_code_in_the_real_config():
    # Guards this module's fixtures against a config change: if SEEDED_SECTOR_CODE were removed
    # from config/sector_profiles.yaml or moved to another profile, every run() test below would
    # abort or produce unsupported_business_model for a reason unrelated to what it tests.
    config = load_sector_profiles(DEFAULT_SECTOR_PROFILES_PATH)
    assignment = config.assignment(SEEDED_SECTOR_CODE)
    assert assignment is not None, (
        f"sector code {SEEDED_SECTOR_CODE} (used by this module's seeded survivors) is not listed in "
        f"{DEFAULT_SECTOR_PROFILES_PATH}; pick another standard-profile code for SEEDED_SECTOR_CODE"
    )
    assert assignment.profile.name == "non_financial" and assignment.profile.supported, (
        f"sector code {SEEDED_SECTOR_CODE} must map to the supported profile 'non_financial' in "
        f"{DEFAULT_SECTOR_PROFILES_PATH} (this module's tests evaluate its survivors through Stage 1), "
        f"got {assignment.profile.name!r}"
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


def seed_db(db_path, *, run_timestamp=RUN_TS, survivors=(), excluded_ids=()):
    """survivors: [(simfin_id, ticker), ...]."""
    results = [passed(i, ticker) for i, ticker in survivors] + [excluded(i, f"X{i}") for i in excluded_ids]
    save_universe_prefilter(run_timestamp, results, config_hash="deadbeef", db_path=db_path)


def seed_cache(cache_dir, simfin_id, response, *, outcome=sf.FETCH_OK, statements=sf.DEFAULT_STATEMENTS):
    sf._write_envelope(
        sf.cache_path(cache_dir, simfin_id),
        {
            "kind": "fundamentals_raw",
            "simfin_id": simfin_id,
            "statements": list(statements),
            "outcome": outcome,
            "fetched_at_utc": "x",
            "rate_limit_remaining": None,
            "response": response,
        },
    )


def _read_table(db_path, table):
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        return {row["ticker"]: dict(row) for row in connection.execute(f"SELECT * FROM {table}")}
    finally:
        connection.close()


def do_run(tmp_path, **overrides):
    """rsf.run() with every path pinned under tmp_path - never the module
    defaults, which point at the real data/run_archive.db and the real
    fundamentals cache (see module docstring)."""
    kwargs = dict(
        db_path=tmp_path / "archive.db",
        archive_db_path=tmp_path / "archive.db",
        csv_dir=tmp_path / "runs",
        fundamentals_cache_dir=tmp_path / "cache",
        out=io.StringIO(),
    )
    kwargs.update(overrides)
    out = kwargs["out"]
    exit_code = rsf.run(**kwargs)
    return exit_code, out.getvalue()


# --- load_survivors ------------------------------------------------------------


def test_load_survivors_delegates_to_load_survivor_ids(tmp_path):
    db_path = tmp_path / "archive.db"
    seed_db(db_path, survivors=[(3, "T3"), (1, "T1"), (2, "T2")])
    from run_fundamentals import load_survivor_ids

    assert rsf.load_survivors(db_path) == load_survivor_ids(db_path)
    assert rsf.load_survivors(db_path, RUN_TS) == load_survivor_ids(db_path, RUN_TS)


def test_load_survivors_raises_when_archive_is_missing(tmp_path):
    with pytest.raises(rsf.Stage1FullRunError, match="not found"):
        rsf.load_survivors(tmp_path / "nope.db")


def test_load_survivors_raises_when_no_run_has_passed_rows(tmp_path):
    db_path = tmp_path / "archive.db"
    seed_db(db_path, survivors=[], excluded_ids=(4,))
    with pytest.raises(rsf.Stage1FullRunError, match="no universe_prefilter run"):
        rsf.load_survivors(db_path)


# --- evaluate_from_cache ---------------------------------------------------------


def test_evaluate_from_cache_ok_matches_evaluate_ticker_directly(tmp_path):
    raw = _load_raw("UFPT")
    cache_dir = tmp_path / "cache"
    seed_cache(cache_dir, raw["id"], raw)
    result = rsf.evaluate_from_cache("UFPT", raw["id"], cache_dir=cache_dir, thresholds=DEFAULT_THRESHOLDS)
    expected = run_stage1.evaluate_ticker("UFPT", raw, thresholds=DEFAULT_THRESHOLDS)
    assert result == expected


def test_evaluate_from_cache_no_cache_file_is_eliminated_as_statements_missing(tmp_path):
    cache_dir = tmp_path / "cache"
    result = rsf.evaluate_from_cache("T1", 1, cache_dir=cache_dir, thresholds=DEFAULT_THRESHOLDS)
    assert result.status == run_stage1.NOT_EVALUABLE
    assert result.invalid_reasons == (rsf.SIMFIN_STATEMENTS_MISSING,)


def test_evaluate_from_cache_outcome_absent_is_eliminated_as_statements_missing(tmp_path):
    cache_dir = tmp_path / "cache"
    seed_cache(cache_dir, 1, None, outcome=sf.FETCH_ABSENT)
    result = rsf.evaluate_from_cache("T1", 1, cache_dir=cache_dir, thresholds=DEFAULT_THRESHOLDS)
    assert result.invalid_reasons == (rsf.SIMFIN_STATEMENTS_MISSING,)


def test_evaluate_from_cache_corrupt_json_is_eliminated_as_cache_invalid(tmp_path):
    cache_dir = tmp_path / "cache"
    path = sf.cache_path(cache_dir, 1)
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")
    result = rsf.evaluate_from_cache("T1", 1, cache_dir=cache_dir, thresholds=DEFAULT_THRESHOLDS)
    assert result.status == run_stage1.NOT_EVALUABLE
    assert result.invalid_reasons == (rsf.SIMFIN_STATEMENTS_CACHE_INVALID,)


def test_evaluate_from_cache_mismatched_envelope_is_eliminated_as_cache_invalid(tmp_path):
    cache_dir = tmp_path / "cache"
    seed_cache(cache_dir, 1, {"id": 999, "ticker": "WRONG"})
    # write under simfin_id=1's path but with a mismatched simfin_id inside
    sf._write_envelope(
        sf.cache_path(cache_dir, 1),
        {
            "kind": "fundamentals_raw",
            "simfin_id": 999,  # mismatched
            "statements": list(sf.DEFAULT_STATEMENTS),
            "outcome": sf.FETCH_OK,
            "fetched_at_utc": "x",
            "rate_limit_remaining": None,
            "response": {"id": 999},
        },
    )
    result = rsf.evaluate_from_cache("T1", 1, cache_dir=cache_dir, thresholds=DEFAULT_THRESHOLDS)
    assert result.invalid_reasons == (rsf.SIMFIN_STATEMENTS_CACHE_INVALID,)


def test_evaluate_from_cache_a_none_ticker_is_eliminated_without_touching_the_cache(tmp_path):
    # Checked first, before any cache read (review fix, 2026-09-30):
    # evaluate_ticker()/build_company_record() would otherwise crash on
    # ticker.upper() when the cached response carries its own non-null
    # ticker. cache_dir does not even need to exist for this to hold.
    cache_dir = tmp_path / "cache"
    result = rsf.evaluate_from_cache(None, 1, cache_dir=cache_dir, thresholds=DEFAULT_THRESHOLDS)
    assert result.status == run_stage1.NOT_EVALUABLE
    assert result.invalid_reasons == (rsf.TICKER_MISSING,)


def test_evaluate_from_cache_a_none_ticker_does_not_crash_even_with_a_real_cached_response(tmp_path):
    # The concrete crash the review found: a cached response whose OWN
    # "ticker" field is a real (non-null) string used to crash inside
    # build_company_record's ticker.upper() comparison when the ticker
    # ARGUMENT was None - reproduced here with a real pilot fixture.
    raw = _load_raw("UFPT")
    cache_dir = tmp_path / "cache"
    seed_cache(cache_dir, raw["id"], raw)
    result = rsf.evaluate_from_cache(None, raw["id"], cache_dir=cache_dir, thresholds=DEFAULT_THRESHOLDS)
    assert result.invalid_reasons == (rsf.TICKER_MISSING,)


# --- run(): end-to-end -----------------------------------------------------------


def test_run_end_to_end_happy_path_writes_run_results_and_csv_from_cache_only(tmp_path):
    ufpt, atro = _load_raw("UFPT"), _load_raw("ATRO")
    db_path = tmp_path / "archive.db"
    seed_db(db_path, survivors=[(ufpt["id"], "UFPT"), (atro["id"], "ATRO")])
    cache_dir = tmp_path / "cache" / "2026-09-24"
    seed_cache(cache_dir, ufpt["id"], ufpt)
    seed_cache(cache_dir, atro["id"], atro)

    exit_code, output = do_run(tmp_path, db_path=db_path)

    assert exit_code == rsf.EXIT_OK
    survivors = _read_table(db_path, "run_results")
    eliminated = _read_table(db_path, "eliminated_results")
    assert set(survivors) | set(eliminated) == {"UFPT", "ATRO"}
    assert not set(survivors) & set(eliminated)

    runs_row = sqlite3.connect(db_path).execute(
        "SELECT companies_evaluated, survivors_count FROM runs"
    ).fetchone()
    assert runs_row == (2, len(survivors))

    csv_files = list((tmp_path / "runs").glob("survivors_*.csv"))
    assert len(csv_files) == 1
    assert "source_run_timestamp=2026-09-24T18:13:48" in output
    assert "2 companies:" in output


def test_run_generates_a_fresh_archive_run_timestamp_distinct_from_the_source(tmp_path):
    raw = _load_raw("UFPT")
    db_path = tmp_path / "archive.db"
    seed_db(db_path, survivors=[(raw["id"], "UFPT")])
    cache_dir = tmp_path / "cache" / "2026-09-24"
    seed_cache(cache_dir, raw["id"], raw)

    exit_code, output = do_run(tmp_path, db_path=db_path)

    assert exit_code == rsf.EXIT_OK
    runs_row = sqlite3.connect(db_path).execute("SELECT run_timestamp FROM runs").fetchone()
    archive_run_timestamp = runs_row[0]
    assert archive_run_timestamp != RUN_TS
    assert f"source_run_timestamp={RUN_TS}" in output
    assert f"run_timestamp={archive_run_timestamp}" in output


def test_run_as_of_defaults_to_the_source_run_timestamps_date_not_today(tmp_path):
    raw = _load_raw("UFPT")
    db_path = tmp_path / "archive.db"
    seed_db(db_path, survivors=[(raw["id"], "UFPT")])
    cache_dir = tmp_path / "cache" / "2026-09-24"
    seed_cache(cache_dir, raw["id"], raw)

    exit_code, output = do_run(tmp_path, db_path=db_path)

    assert exit_code == rsf.EXIT_OK
    assert "as_of=2026-09-24" in output


def test_run_explicit_as_of_overrides_the_default(tmp_path):
    from datetime import date

    raw = _load_raw("UFPT")
    db_path = tmp_path / "archive.db"
    seed_db(db_path, survivors=[(raw["id"], "UFPT")])
    cache_dir = tmp_path / "cache" / "2026-09-20"  # a different date than RUN_TS's
    seed_cache(cache_dir, raw["id"], raw)

    exit_code, output = do_run(tmp_path, db_path=db_path, as_of=date(2026, 9, 20))

    assert exit_code == rsf.EXIT_OK
    assert "as_of=2026-09-20" in output
    survivors = _read_table(db_path, "run_results")
    eliminated = _read_table(db_path, "eliminated_results")
    assert set(survivors) | set(eliminated) == {"UFPT"}  # the cache under 2026-09-20 was actually read


def test_run_a_missing_survivors_archive_is_a_technical_failure(tmp_path):
    exit_code, output = do_run(tmp_path, db_path=tmp_path / "nope.db")
    assert exit_code == rsf.EXIT_TECHNICAL_FAILURE
    assert not (tmp_path / "archive.db").exists()
    assert not list((tmp_path / "runs").glob("*.csv")) if (tmp_path / "runs").exists() else True


def test_run_an_invalid_thresholds_file_is_a_technical_failure(tmp_path):
    exit_code, output = do_run(tmp_path, thresholds_path=tmp_path / "nope.yaml")
    assert exit_code == rsf.EXIT_TECHNICAL_FAILURE


def test_run_rolls_back_the_csv_if_the_archive_db_write_then_fails(monkeypatch, tmp_path):
    raw = _load_raw("UFPT")
    db_path = tmp_path / "archive.db"
    seed_db(db_path, survivors=[(raw["id"], "UFPT")])
    cache_dir = tmp_path / "cache" / "2026-09-24"
    seed_cache(cache_dir, raw["id"], raw)

    def _raise(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(rsf, "save_run_results", _raise)

    exit_code, output = do_run(tmp_path, db_path=db_path)

    assert exit_code == rsf.EXIT_TECHNICAL_FAILURE
    assert list((tmp_path / "runs").glob("*.csv")) == []


def test_run_a_corrupt_cache_file_eliminates_that_company_without_aborting(tmp_path):
    ufpt = _load_raw("UFPT")
    db_path = tmp_path / "archive.db"
    seed_db(db_path, survivors=[(ufpt["id"], "UFPT"), (999, "BROKEN")])
    cache_dir = tmp_path / "cache" / "2026-09-24"
    seed_cache(cache_dir, ufpt["id"], ufpt)
    broken_path = sf.cache_path(cache_dir, 999)
    broken_path.parent.mkdir(parents=True, exist_ok=True)
    broken_path.write_text("{ not json", encoding="utf-8")

    exit_code, output = do_run(tmp_path, db_path=db_path)

    assert exit_code == rsf.EXIT_OK
    eliminated = _read_table(db_path, "eliminated_results")
    assert eliminated["BROKEN"]["invalid_reasons"] == rsf.SIMFIN_STATEMENTS_CACHE_INVALID
    survivors = _read_table(db_path, "run_results")
    assert "UFPT" in survivors or "UFPT" in eliminated  # unaffected either way, not swallowed


def test_run_two_survivors_sharing_a_ticker_is_a_technical_failure_nothing_written(tmp_path):
    # run_results/eliminated_results key on (run_timestamp, ticker);
    # universe_prefilter has no such uniqueness (its key is (run_timestamp,
    # simfin_id)) - two simfin_ids sharing one ticker must abort loudly,
    # not silently let one overwrite the other in gate_results (review fix,
    # 2026-09-30).
    ufpt = _load_raw("UFPT")
    db_path = tmp_path / "archive.db"
    seed_db(db_path, survivors=[(ufpt["id"], "DUP"), (999999, "DUP")])
    cache_dir = tmp_path / "cache" / "2026-09-24"
    seed_cache(cache_dir, ufpt["id"], ufpt)
    seed_cache(cache_dir, 999999, dict(ufpt, id=999999))

    exit_code, output = do_run(tmp_path, db_path=db_path)

    assert exit_code == rsf.EXIT_TECHNICAL_FAILURE
    # db_path already exists (seed_db wrote universe_prefilter into it) -
    # the assertion is that NO run_results/eliminated_results/runs rows
    # were ever written, i.e. save_run_results() was never reached.
    assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)
    assert list((tmp_path / "runs").glob("*.csv")) == []


def test_run_a_survivor_with_no_ticker_is_excluded_from_the_archive_and_reported(tmp_path):
    # universe_prefilter.ticker is nullable; run_results/eliminated_results.
    # ticker is NOT NULL, so a null-ticker survivor cannot be archived
    # either way. Must not crash and must not silently vanish - excluded
    # and reported, the rest of the run still succeeds (review fix,
    # 2026-09-30).
    ufpt = _load_raw("UFPT")
    db_path = tmp_path / "archive.db"
    seed_db(db_path, survivors=[(ufpt["id"], "UFPT"), (999999, None)])
    cache_dir = tmp_path / "cache" / "2026-09-24"
    seed_cache(cache_dir, ufpt["id"], ufpt)
    seed_cache(cache_dir, 999999, dict(ufpt, id=999999))

    exit_code, output = do_run(tmp_path, db_path=db_path)

    assert exit_code == rsf.EXIT_OK
    survivors = _read_table(db_path, "run_results")
    eliminated = _read_table(db_path, "eliminated_results")
    assert set(survivors) | set(eliminated) == {"UFPT"}  # only the archivable one landed anywhere
    assert "ACHTUNG" in output and "999999" in output  # the exclusion is reported, not silent
    runs_row = sqlite3.connect(db_path).execute("SELECT companies_evaluated FROM runs").fetchone()
    assert runs_row == (1,)  # companies_evaluated reflects what was actually archived


# --- build_parser() / main() ------------------------------------------------------


def test_build_parser_has_the_documented_flags_and_defaults():
    parser = rsf.build_parser()
    flags = {action.dest for action in parser._actions if action.option_strings}
    assert flags == {
        "help", "run_timestamp", "as_of", "fundamentals_cache_dir", "db_path", "archive_db_path", "csv_dir",
        "sector_profiles_path",
    }
    args = parser.parse_args([])
    assert args.run_timestamp is None
    assert args.as_of is None
    assert args.fundamentals_cache_dir == rsf.DEFAULT_FUNDAMENTALS_CACHE_DIR
    assert args.db_path == rsf.DEFAULT_DB_PATH
    assert args.archive_db_path == rsf.DEFAULT_DB_PATH
    assert args.csv_dir == rsf.DEFAULT_CSV_DIR
    assert args.sector_profiles_path == DEFAULT_SECTOR_PROFILES_PATH


def test_main_passes_the_arguments_through_to_run(monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return rsf.EXIT_OK

    monkeypatch.setattr(rsf, "run", fake_run)
    exit_code = rsf.main(["--run-timestamp", "2026-09-24T18:13:48", "--as-of", "2026-09-24"])

    assert exit_code == rsf.EXIT_OK
    assert captured["run_timestamp"] == "2026-09-24T18:13:48"
    from datetime import date

    assert captured["as_of"] == date(2026, 9, 24)
    assert captured["db_path"] == rsf.DEFAULT_DB_PATH
    assert captured["archive_db_path"] == rsf.DEFAULT_DB_PATH
    assert captured["csv_dir"] == rsf.DEFAULT_CSV_DIR
    assert captured["fundamentals_cache_dir"] == rsf.DEFAULT_FUNDAMENTALS_CACHE_DIR


# --- 16-pilot regression proxy (in-suite invariant check, see also the ------------
# read-only regression against the external baseline run separately, per
# docs/decisions.md "Phase 2.0c" Entscheidung 5) --------------------------------


def test_16_pilot_cache_path_matches_live_evaluate_ticker_exactly(tmp_path):
    db_path = tmp_path / "archive.db"
    cache_dir = tmp_path / "cache" / "2026-09-24"
    raws = {ticker: _load_raw(ticker) for ticker in PILOTS_IN_CACHE}
    seed_db(db_path, survivors=[(raw["id"], ticker) for ticker, raw in raws.items()])
    for ticker, raw in raws.items():
        seed_cache(cache_dir, raw["id"], raw)

    exit_code, output = do_run(tmp_path, db_path=db_path)
    assert exit_code == rsf.EXIT_OK

    survivors = _read_table(db_path, "run_results")
    eliminated = _read_table(db_path, "eliminated_results")
    written = {**survivors, **eliminated}
    assert set(written) == set(PILOTS_IN_CACHE)

    for ticker, raw in raws.items():
        expected = run_stage1.evaluate_ticker(ticker, raw, thresholds=DEFAULT_THRESHOLDS)
        row = written[ticker]
        assert row["status"] == expected.status
        if ticker in survivors:  # run_results: roic_3yr/net_debt_ebitda/cash_conversion_3yr
            assert row["roic_3yr"] == expected.roic
            assert row["net_debt_ebitda"] == expected.net_debt_ebitda
            assert row["cash_conversion_3yr"] == expected.cash_conversion
        else:  # eliminated_results: roic/net_debt_ebitda/cash_conversion/net_income_margin/invalid_reasons
            assert row["roic"] == expected.roic
            assert row["net_debt_ebitda"] == expected.net_debt_ebitda
            assert row["cash_conversion"] == expected.cash_conversion
            assert row["net_income_margin"] == expected.net_income_margin
            assert row["invalid_reasons"] == ",".join(expected.invalid_reasons)

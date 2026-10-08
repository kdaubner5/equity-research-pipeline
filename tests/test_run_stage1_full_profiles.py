"""run_stage1_full.py with the sector profiles (Issue #51, docs/decisions.md "Phase 2.0e").

Offline like test_run_stage1_full.py: every path is pinned under tmp_path, the profile
YAML is a small one written per test (not the real config), nothing touches the real
data/run_archive.db or the network."""

import io
import sqlite3

import pytest

from config_layer.sector_profiles import DEFAULT_SECTOR_PROFILES_PATH
from gates_layer.universe_prefilter import PASSED, PrefilterResult
from store_layer.db import save_universe_prefilter

import run_stage1
import run_stage1_full as rsf

from test_run_stage1_full import RUN_TS, _load_raw, _read_table, seed_cache

PROFILES_YAML = """
profiles:
  non_financial:
    supported: true
  banks:
    supported: false
    reason: "Banken (D2)"
sectors:
  101003: {profile: non_financial, borderline: false}
  104001: {profile: non_financial, borderline: true}
  104002: {profile: banks, borderline: false}
"""
STANDARD, BORDERLINE, BANK = 101003, 104001, 104002


def prefilter_row(simfin_id, ticker, sector_code):
    return PrefilterResult(
        simfin_id=simfin_id, ticker=ticker, sector_code=sector_code, status=PASSED, reason=None,
        market_cap_usd=1_000_000_000, median_dollar_volume_usd=2_000_000, median_window_rows=60, price_date=None,
    )


def setup_run(tmp_path, survivors, *, cached=()):
    """survivors: [(simfin_id, ticker, sector_code), ...]; cached: raw-fixture tickers to put in the cache."""
    profiles_path = tmp_path / "sector_profiles.yaml"
    profiles_path.write_text(PROFILES_YAML, encoding="utf-8")
    db_path = tmp_path / "archive.db"
    save_universe_prefilter(
        RUN_TS, [prefilter_row(*row) for row in survivors], config_hash="deadbeef", db_path=db_path, rule_state=None
    )
    cache_dir = tmp_path / "cache" / "2026-09-24"
    for ticker in cached:
        raw = _load_raw(ticker)
        seed_cache(cache_dir, raw["id"], raw)
    return profiles_path, db_path


def do_run(tmp_path, profiles_path, db_path):
    out = io.StringIO()
    exit_code = rsf.run(
        db_path=db_path,
        archive_db_path=db_path,
        csv_dir=tmp_path / "runs",
        fundamentals_cache_dir=tmp_path / "cache",
        sector_profiles_path=profiles_path,
        out=out,
    )
    return exit_code, out.getvalue()


def _archive_is_untouched(tmp_path, db_path):
    connection = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("runs", "run_results", "eliminated_results")
            if table in tables
        }
    finally:
        connection.close()
    assert all(count == 0 for count in counts.values()), counts
    assert not (tmp_path / "runs").exists() or not list((tmp_path / "runs").glob("*"))


def test_an_unknown_sector_code_aborts_before_any_evaluation_and_names_the_codes(tmp_path, capsys):
    ufpt = _load_raw("UFPT")
    profiles_path, db_path = setup_run(
        tmp_path, [(ufpt["id"], "UFPT", STANDARD), (900, "NEWCO", 999999), (901, "NEWCO2", 888888)], cached=["UFPT"]
    )

    exit_code, output = do_run(tmp_path, profiles_path, db_path)

    assert exit_code == rsf.EXIT_TECHNICAL_FAILURE
    stderr = capsys.readouterr().err
    assert "888888" in stderr and "999999" in stderr  # every missing code is named
    assert "sector_profiles" in stderr
    _archive_is_untouched(tmp_path, db_path)  # nothing written: no runs row, no results, no CSV


def test_a_null_sector_code_aborts_too_and_is_reported(tmp_path, capsys):
    ufpt = _load_raw("UFPT")
    profiles_path, db_path = setup_run(tmp_path, [(ufpt["id"], "UFPT", STANDARD), (900, "NOCODE", None)], cached=["UFPT"])

    exit_code, _ = do_run(tmp_path, profiles_path, db_path)

    assert exit_code == rsf.EXIT_TECHNICAL_FAILURE
    assert "NULL" in capsys.readouterr().err
    _archive_is_untouched(tmp_path, db_path)


def test_an_invalid_profiles_file_is_a_technical_failure_and_writes_nothing(tmp_path, capsys):
    ufpt = _load_raw("UFPT")
    profiles_path, db_path = setup_run(tmp_path, [(ufpt["id"], "UFPT", STANDARD)], cached=["UFPT"])
    profiles_path.write_text("profiles: {}\n", encoding="utf-8")

    exit_code, _ = do_run(tmp_path, profiles_path, db_path)

    assert exit_code == rsf.EXIT_TECHNICAL_FAILURE
    _archive_is_untouched(tmp_path, db_path)
    exit_code, _ = do_run(tmp_path, tmp_path / "missing.yaml", db_path)
    assert exit_code == rsf.EXIT_TECHNICAL_FAILURE


def test_unsupported_profile_is_not_evaluated_and_archived_with_its_profile(tmp_path):
    ufpt = _load_raw("UFPT")
    # the bank has NO cache file: were it evaluated it would be simfin_statements_missing
    profiles_path, db_path = setup_run(
        tmp_path, [(ufpt["id"], "UFPT", STANDARD), (900, "BANKCO", BANK)], cached=["UFPT"]
    )

    exit_code, output = do_run(tmp_path, profiles_path, db_path)

    assert exit_code == rsf.EXIT_OK
    eliminated = _read_table(db_path, "eliminated_results")
    bank = eliminated["BANKCO"]
    assert bank["status"] == "unsupported_business_model"
    assert bank["invalid_reasons"] == ""
    assert bank["profile"] == "banks"
    assert bank["borderline_sector"] == 0
    assert all(bank[column] is None for column in ("roic", "net_debt_ebitda", "cash_conversion", "net_income_margin"))
    survivors = _read_table(db_path, "run_results")
    assert set(survivors) == {"UFPT"}
    assert (survivors["UFPT"]["profile"], survivors["UFPT"]["borderline_sector"]) == ("non_financial", 0)
    assert "unsupported_business_model=1" in output
    assert "borderline_sector=0" in output
    assert "2 companies:" in output


def test_borderline_sector_is_marked_but_changes_neither_status_nor_flags_nor_rank(tmp_path):
    ufpt, flxs = _load_raw("UFPT"), _load_raw("FLXS")
    plain_dir, marked_dir = tmp_path / "plain", tmp_path / "marked"
    for directory, codes in ((plain_dir, (STANDARD, STANDARD)), (marked_dir, (BORDERLINE, BORDERLINE))):
        directory.mkdir()
        profiles_path, db_path = setup_run(
            directory, [(ufpt["id"], "UFPT", codes[0]), (flxs["id"], "FLXS", codes[1])], cached=["UFPT", "FLXS"]
        )
        assert do_run(directory, profiles_path, db_path)[0] == rsf.EXIT_OK

    plain = _read_table(plain_dir / "archive.db", "run_results")
    marked = _read_table(marked_dir / "archive.db", "run_results")
    assert set(plain) == set(marked) == {"UFPT", "FLXS"}
    for ticker in plain:
        for column in ("status", "flags", "rank_position", "roic_3yr", "net_debt_ebitda", "cash_conversion_3yr"):
            assert plain[ticker][column] == marked[ticker][column], (ticker, column)
        assert plain[ticker]["borderline_sector"] == 0
        assert marked[ticker]["borderline_sector"] == 1


def test_default_profiles_path_is_the_real_config_and_is_a_cli_option():
    args = rsf.build_parser().parse_args([])
    assert args.sector_profiles_path == DEFAULT_SECTOR_PROFILES_PATH
    assert rsf.build_parser().parse_args(["--sector-profiles-path", "x.yaml"]).sector_profiles_path.name == "x.yaml"


# --- the pilot runner stays without a profile check ----------------------------------


def test_summary_of_a_run_without_profiles_says_not_checked_and_has_a_zero_for_the_fifth_status():
    from gates_layer.stage1_gates import unsupported_business_model_result

    out = io.StringIO()
    run_stage1.print_summary({}, file=out)
    text = out.getvalue()
    assert "unsupported_business_model=0" in text
    assert "profile check: not checked" in text

    out = io.StringIO()
    run_stage1.print_summary({"B": unsupported_business_model_result("banks")}, file=out)
    text = out.getvalue()
    assert "unsupported_business_model=1" in text
    assert "profile check: not checked" not in text


# --- hardening from the /code-review of PR #53 ----------------------------------------


def test_sector_codes_can_be_read_from_a_path_containing_hash_and_percent(tmp_path):
    # the read-only SQLite URI must be built with a percent-encoded path (Path.as_uri()), else '#'
    # starts the URI fragment and '%41' would decode to 'A'
    odd_dir = tmp_path / "run#2 %41 dir"
    odd_dir.mkdir()
    db_path = odd_dir / "archive.db"
    save_universe_prefilter(
        RUN_TS, [prefilter_row(1, "AAA", STANDARD), prefilter_row(2, "BBB", BANK)], config_hash="deadbeef", db_path=db_path, rule_state=None
    )
    assert rsf.load_survivor_sector_codes(db_path, RUN_TS) == {1: STANDARD, 2: BANK}


def test_the_loop_guard_raises_a_run_error_with_the_simfin_id_if_the_coverage_check_was_bypassed(
    monkeypatch, tmp_path, capsys
):
    # The coverage check catches an unlisted/NULL code before the loop today; the guard in the loop is
    # only a safeguard. Trigger it directly: a sector-code source that lacks one survivor, with the
    # coverage check disabled.
    ufpt = _load_raw("UFPT")
    profiles_path, db_path = setup_run(tmp_path, [(ufpt["id"], "UFPT", STANDARD), (900, "GHOST", STANDARD)], cached=["UFPT"])
    monkeypatch.setattr(rsf, "load_survivor_sector_codes", lambda *args, **kwargs: {ufpt["id"]: STANDARD})
    monkeypatch.setattr(rsf, "check_sector_profile_coverage", lambda *args, **kwargs: None)

    exit_code, _ = do_run(tmp_path, profiles_path, db_path)

    assert exit_code == rsf.EXIT_TECHNICAL_FAILURE  # a run error, not an AttributeError traceback
    assert "900" in capsys.readouterr().err
    _archive_is_untouched(tmp_path, db_path)

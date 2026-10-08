"""End-to-end tests for run_trend_guard.py (docs/decisions.md "Trend-Guard (Masterplan Stufe 1.1, Hebel 3 aus
Issue #52)", Abschnitt 3 und 8, Test 4): offline, deterministic, no network.

Test data: the 23 committed pilot raw-SimFin fixtures as the fundamentals cache, artificial prefilter rows and
artificial Stage-1 survivors, all in tmp_path (the World of test_run_stage2). Every run() call passes
tmp_path-based paths explicitly - this file never touches data/ or a real archive."""

import copy
import hashlib
import io
import sqlite3
from pathlib import Path

import pytest

from data_layer import simfin_fundamentals as sf
from gates_layer.stage2_gates import Stage2Result
from store_layer.stage2_store import save_stage2_results
from test_run_stage2 import PREFILTER_TS, STAGE1_TS, World, _set_cell, prefilter_row, raw, seed_cache

import run_trend_guard

TREND_TS = "2026-10-08T12:00:00"


class TrendWorld(World):
    def run(self, **overrides):
        kwargs = dict(
            db_path=self.source_db, archive_db_path=self.archive_db, csv_dir=self.csv_dir,
            fundamentals_cache_dir=self.cache_root, stage1_run_timestamp=STAGE1_TS, run_timestamp=TREND_TS,
            out=io.StringIO(),
        )
        kwargs.update(overrides)
        out = kwargs["out"]
        code = run_trend_guard.run(**kwargs)
        return code, out.getvalue()

    def trend(self, sql):
        return self.stage2(sql)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def nothing_written(world):
    return not world.archive_db.exists() and not world.csv_dir.exists()


def result_rows(world):
    return {
        r[0]: r[1:]
        for r in world.trend("SELECT ticker, status, marks, mark_count, revenue_invalid_reason FROM trend_results")
    }


# ---------------------------------------------------------------- happy path


def test_full_run_evaluates_exactly_the_stage1_survivors(tmp_path):
    world = TrendWorld(tmp_path)
    code, out = world.run()
    assert code == run_trend_guard.EXIT_OK, out
    assert world.trend(
        "SELECT stage1_run_timestamp, prefilter_run_timestamp, as_of, companies_evaluated FROM trend_runs"
    ) == [(STAGE1_TS, PREFILTER_TS, "2026-09-24", 3)]
    got = result_rows(world)
    assert set(got) == {"CENT", "SCSC", "KELYA"}
    assert got["KELYA"][0] == "trend_not_evaluable"  # no periods at all
    assert got["KELYA"][3] == "no_periods"
    assert {got["CENT"][0], got["SCSC"][0]} <= {"trend_clear", "trend_marked", "trend_not_evaluable"}
    assert len(list(world.csv_dir.glob("trend_guard_*.csv"))) == 1


def test_all_survivors_are_evaluated_also_those_that_fail_stage2(tmp_path):
    survivors = ("CENT", "SCSC", "UFPT", "PLOW", "FLXS", "CVGI", "KELYA")
    world = TrendWorld(tmp_path, survivors=survivors)
    assert world.run()[0] == 0
    assert set(result_rows(world)) == set(survivors)


def test_summary_names_the_counts_with_their_reference(tmp_path):
    world = TrendWorld(tmp_path)
    _, out = world.run()
    assert "survivors_evaluated=3" in out
    assert "status (von 3 Ueberlebenden)" in out
    assert "marken (Firmen, von 3 Ueberlebenden)" in out
    for mark in ("trend_revenue_falling", "trend_margin_falling", "trend_roic_below_floor", "trend_margin_peak"):
        assert f"{mark}=" in out  # every mark is named, also with count 0
    assert "mit_mindestens_einer_marke:" in out and "von 3 Ueberlebenden" in out
    assert "no_periods=1" in out  # one company, not two entries


def test_the_summary_names_the_cache_directory_that_was_read(tmp_path):
    world = TrendWorld(tmp_path)
    _, out = world.run()
    assert str(world.cache_dir) in out


def test_the_source_archive_is_not_modified_when_results_go_elsewhere(tmp_path):
    world = TrendWorld(tmp_path)
    before = sha(world.source_db)
    assert world.run()[0] == 0
    assert sha(world.source_db) == before


def test_results_do_not_depend_on_the_order_of_the_survivors(tmp_path):
    first = TrendWorld(tmp_path / "a", survivors=("CENT", "SCSC", "UFPT"))
    second = TrendWorld(tmp_path / "b", survivors=("UFPT", "SCSC", "CENT"))
    first.run()
    second.run()
    sql = "SELECT ticker, status, marks, revenue_ratio, ebitda_margin_ratio, roic_last_year FROM trend_results ORDER BY ticker"
    assert first.trend(sql) == second.trend(sql)


def test_a_longer_window_from_the_config_changes_the_ratios_but_not_the_survivors(tmp_path):
    world = TrendWorld(tmp_path)
    cfg = tmp_path / "trend5.yaml"
    cfg.write_text(
        "trend_guard:\n  years: 5\n  min_years: 3\n  revenue_ratio_flag_below: 0.90\n"
        "  ebitda_margin_ratio_flag_below: 0.90\n  ebitda_margin_ratio_flag_above: 1.25\n",
        encoding="utf-8",
    )
    code, out = world.run(trend_config_path=cfg)
    assert code == 0, out
    assert set(result_rows(world)) == {"CENT", "SCSC", "KELYA"}


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
    world = TrendWorld(tmp_path, survivors=("MISS", "CORR", "ABSE", "MAPE", "UFPT"), extra_prefilter=extra)
    seed_cache(world.cache_dir, 9003, None, outcome=sf.FETCH_ABSENT)
    seed_cache(world.cache_dir, 9004, broken)
    sf.cache_path(world.cache_dir, 9002).parent.mkdir(parents=True, exist_ok=True)
    sf.cache_path(world.cache_dir, 9002).write_text("{not json", encoding="utf-8")

    code, out = world.run()
    assert code == 0, out
    reasons = {r[0]: (r[1], r[2]) for r in world.trend("SELECT ticker, status, revenue_invalid_reason FROM trend_results")}
    assert reasons["MISS"] == ("trend_not_evaluable", "fundamentals_cache_missing")
    assert reasons["ABSE"] == ("trend_not_evaluable", "fundamentals_cache_missing")
    assert reasons["CORR"] == ("trend_not_evaluable", "fundamentals_cache_invalid")
    assert reasons["MAPE"] == ("trend_not_evaluable", "simfin_mapping_error")
    assert reasons["UFPT"][1] is None  # the intact survivor is evaluated
    assert "fundamentals_cache_invalid" in out and "simfin_mapping_error" in out


def test_a_survivor_missing_from_the_prefilter_is_reported_not_skipped(tmp_path):
    world = TrendWorld(tmp_path, survivors=("CENT", "NOTINUNIVERSE"))
    code, out = world.run()
    assert code == 0
    assert world.trend("SELECT status, revenue_invalid_reason, simfin_id FROM trend_results WHERE ticker = 'NOTINUNIVERSE'") == [
        ("trend_not_evaluable", "not_in_universe_prefilter", None)
    ]
    assert "not_in_universe_prefilter" in out


def _malformed_world(tmp_path, mutate):
    world = TrendWorld(tmp_path, survivors=("CENT", "SCSC"))
    record = raw("CENT")
    mutate(record)
    seed_cache(world.cache_dir, record["id"], record)
    return world


@pytest.mark.parametrize(
    "statement,column,value",
    [
        ("PL", "Revenue", "x"),
        ("PL", "Operating Income (Loss)", True),
        ("CF", "Depreciation & Amortization", "x"),
        ("BS", "Total Assets", "x"),
        ("BS", "Total Equity", [1]),
        ("PL", "Revenue", 10**400),
    ],
)
def test_a_malformed_company_gets_an_explicit_reason_and_the_run_goes_on(tmp_path, statement, column, value):
    world = _malformed_world(tmp_path, lambda record: _set_cell(record, statement, column, value))
    code, out = world.run()
    assert code == run_trend_guard.EXIT_OK, out
    rows = {r[0]: r[1:] for r in world.trend("SELECT ticker, status, revenue_invalid_reason FROM trend_results")}
    assert rows["CENT"][0] != "trend_clear"  # never unremarkable
    assert rows["CENT"][1] in {"fundamentals_malformed", "simfin_mapping_error", "non_finite_input"}
    assert rows["SCSC"][1] is None  # the other survivor is evaluated normally
    assert set(rows) == {"CENT", "SCSC"}


def test_nan_in_the_cache_is_never_unremarkable(tmp_path):
    world = _malformed_world(tmp_path, lambda record: _set_cell(record, "PL", "Revenue", float("nan")))
    code, out = world.run()
    assert code == 0, out
    row = world.trend("SELECT status, revenue_invalid_reason, margin_invalid_reason FROM trend_results WHERE ticker = 'CENT'")[0]
    assert row[1] == "non_finite_input" and row[2] == "non_finite_input"  # T1 and T2 cannot be computed
    assert row[0] in {"trend_marked", "trend_not_evaluable"}  # never trend_clear


def test_stale_fundamentals_follow_the_stage2_lag(tmp_path):
    world = TrendWorld(tmp_path, survivors=("CENT",))
    strict = tmp_path / "stage2_lag0.yaml"
    text = Path("config/stage2.yaml").read_text(encoding="utf-8").replace("max_fiscal_year_lag: 1", "max_fiscal_year_lag: 0")
    strict.write_text(text, encoding="utf-8")
    default = TrendWorld(tmp_path / "d", survivors=("CENT",))
    default.run()
    assert default.trend("SELECT revenue_invalid_reason FROM trend_results")[0][0] != "trend_fundamentals_stale"
    world.run(stage2_config_path=strict)
    # the fixtures end at fiscal year 2024 or 2025; lag 0 against as_of 2026 makes them stale
    assert world.trend("SELECT revenue_invalid_reason FROM trend_results") == [("trend_fundamentals_stale",)]


# ---------------------------------------------------------------- technical failures


def test_duplicate_tickers_abort_before_anything_is_written(tmp_path):
    dup = prefilter_row(9999, "CENT")
    world = TrendWorld(tmp_path, extra_prefilter=[dup])
    code, _ = world.run()
    assert code == run_trend_guard.EXIT_TECHNICAL_FAILURE
    assert nothing_written(world)


def test_an_unknown_stage1_run_aborts(tmp_path):
    world = TrendWorld(tmp_path)
    code, _ = world.run(stage1_run_timestamp="2020-01-01T00:00:00")
    assert code == run_trend_guard.EXIT_TECHNICAL_FAILURE and nothing_written(world)


def test_a_missing_source_archive_aborts_and_creates_nothing(tmp_path):
    world = TrendWorld(tmp_path)
    missing = tmp_path / "nope.db"
    code, _ = world.run(db_path=missing)
    assert code == run_trend_guard.EXIT_TECHNICAL_FAILURE
    assert not missing.exists() and nothing_written(world)


def test_an_unknown_prefilter_run_aborts(tmp_path):
    world = TrendWorld(tmp_path)
    code, _ = world.run(prefilter_run_timestamp="2020-01-01T00:00:00")
    assert code == run_trend_guard.EXIT_TECHNICAL_FAILURE and nothing_written(world)


@pytest.mark.parametrize("which", ["trend", "stage2", "thresholds"])
def test_an_invalid_config_aborts(tmp_path, which):
    world = TrendWorld(tmp_path)
    bad = tmp_path / "bad.yaml"
    bad.write_text("nothing: {}\n", encoding="utf-8")
    key = {"trend": "trend_config_path", "stage2": "stage2_config_path", "thresholds": "thresholds_path"}[which]
    code, _ = world.run(**{key: bad})
    assert code == run_trend_guard.EXIT_TECHNICAL_FAILURE and nothing_written(world)


def test_a_failing_archive_write_removes_the_csv_and_reports_failure(tmp_path, monkeypatch):
    world = TrendWorld(tmp_path)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(run_trend_guard, "save_trend_results", boom)
    code, _ = world.run()
    assert code == run_trend_guard.EXIT_TECHNICAL_FAILURE
    assert not list(world.csv_dir.glob("*.csv"))


def test_running_twice_with_the_same_run_timestamp_is_refused_and_leaves_the_first_run(tmp_path):
    world = TrendWorld(tmp_path)
    assert world.run()[0] == 0
    first_csv = sorted(world.csv_dir.glob("*.csv"))
    first_bytes = first_csv[0].read_bytes()
    code, _ = world.run()
    assert code == run_trend_guard.EXIT_TECHNICAL_FAILURE
    assert world.trend("SELECT COUNT(*) FROM trend_runs") == [(1,)]
    assert sorted(world.csv_dir.glob("*.csv")) == first_csv and first_csv[0].read_bytes() == first_bytes


# ---------------------------------------------------------------- Stage-2 block (optional)


def _stage2_result(status):
    return Stage2Result(
        status=status, flags=(), invalid_reasons=(), ev_ebitda=5.0, ev_ebitda_percentile=0.1, peer_level="fine",
        peer_group=1, peer_rank=1, peer_n=9, peer_n_fine=9, peer_n_coarse=9, fcf_yield=0.08, ev_ebitda_normalized=5.0,
        ev_ebitda_normalized_invalid_reason=None, info_flags=(),
    )


def _seed_stage2(db_path, passed, failed):
    results = {t: _stage2_result("passed") for t in passed} | {t: _stage2_result("failed") for t in failed}
    ranked = [(t, results[t]) for t in passed]
    save_stage2_results(
        "2026-10-07T12:00:00", results=results, ranked=ranked, stage1_run_timestamp=STAGE1_TS,
        prefilter_run_timestamp=PREFILTER_TS, as_of="2026-09-24", config_hash="x", peer_base_size=9,
        simfin_ids={}, db_path=db_path, rule_state=None,
    )


def test_the_stage2_block_reports_the_marks_among_the_passers_with_their_reference(tmp_path):
    world = TrendWorld(tmp_path)
    stage2_db = tmp_path / "stage2.db"
    _seed_stage2(stage2_db, passed=["CENT", "SCSC"], failed=["KELYA"])
    before = sha(stage2_db)
    code, out = world.run(stage2_results_db_path=stage2_db, stage2_run_timestamp="2026-10-07T12:00:00")
    assert code == 0, out
    assert "stage2_bestanden" in out and "von 2 Stage-2-Bestandenen" in out
    assert sha(stage2_db) == before  # read only


def test_the_stage2_block_needs_both_arguments(tmp_path):
    world = TrendWorld(tmp_path)
    code, _ = world.run(stage2_results_db_path=tmp_path / "x.db")
    assert code == run_trend_guard.EXIT_TECHNICAL_FAILURE and nothing_written(world)
    code, _ = world.run(stage2_run_timestamp="2026-10-07T12:00:00")
    assert code == run_trend_guard.EXIT_TECHNICAL_FAILURE and nothing_written(world)


def test_an_unknown_stage2_run_aborts_before_anything_is_written(tmp_path):
    world = TrendWorld(tmp_path)
    stage2_db = tmp_path / "stage2.db"
    _seed_stage2(stage2_db, passed=["CENT"], failed=[])
    code, _ = world.run(stage2_results_db_path=stage2_db, stage2_run_timestamp="1999-01-01T00:00:00")
    assert code == run_trend_guard.EXIT_TECHNICAL_FAILURE and nothing_written(world)


# ---------------------------------------------------------------- command line


def test_no_path_has_a_default_so_the_real_archive_is_never_touched_by_accident():
    parser = run_trend_guard.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    required = {a.dest for a in parser._actions if a.required}
    assert {"db_path", "archive_db_path", "csv_dir", "fundamentals_cache_dir", "stage1_run_timestamp"} <= required


def test_main_runs_end_to_end_from_the_command_line(tmp_path, capsys):
    world = TrendWorld(tmp_path)
    argv = [
        "--db-path", str(world.source_db), "--archive-db-path", str(world.archive_db), "--csv-dir", str(world.csv_dir),
        "--fundamentals-cache-dir", str(world.cache_root), "--stage1-run-timestamp", STAGE1_TS,
        "--run-timestamp", TREND_TS,
    ]
    assert run_trend_guard.main(argv) == 0
    assert "survivors_evaluated=3" in capsys.readouterr().out
    assert world.trend("SELECT COUNT(*) FROM trend_results") == [(3,)]


# ---------------------------------------------------------------- pinned facts about the 23 pilot fixtures


def test_the_23_pilot_fixtures_as_survivors_pin_the_measured_picture(tmp_path):
    """Measured with the Trend-Guard on the committed fixtures (as_of 2026-09-24, lag 1), then pinned."""
    from test_run_stage2 import ALL_PILOTS

    world = TrendWorld(tmp_path, survivors=tuple(ALL_PILOTS))
    code, out = world.run()
    assert code == 0, out
    assert "status (von 23 Ueberlebenden): trend_clear=3  trend_marked=11  trend_not_evaluable=9" in out
    assert (
        "marken (Firmen, von 23 Ueberlebenden): trend_revenue_falling=2  trend_margin_falling=3  "
        "trend_roic_below_floor=7  trend_margin_peak=2" in out
    )
    got = {r[0]: r[1:] for r in world.trend("SELECT ticker, status, marks FROM trend_results")}
    assert got["UFPT"] == ("trend_clear", "") and got["DXPE"] == ("trend_clear", "") and got["PLOW"] == ("trend_clear", "")
    assert got["CVGI"] == ("trend_marked", "trend_revenue_falling,trend_margin_falling,trend_roic_below_floor")
    assert got["FLXS"] == ("trend_marked", "trend_margin_peak")
    assert got["FOSL"] == ("trend_marked", "trend_revenue_falling")  # a mark wins although T2 and T3 cannot be computed
    assert got["KELYA"][0] == "trend_not_evaluable" and got["CATO"][0] == "trend_not_evaluable"
    stale = world.trend("SELECT ticker FROM trend_results WHERE revenue_invalid_reason = 'trend_fundamentals_stale' ORDER BY ticker")
    assert stale == [("CATO",), ("MATW",), ("SGC",)]


def test_the_stage2_block_counts_passers_with_flags_too(tmp_path):
    world = TrendWorld(tmp_path)
    stage2_db = tmp_path / "stage2.db"
    results = {"CENT": _stage2_result("passed_with_flags"), "SCSC": _stage2_result("passed"), "KELYA": _stage2_result("failed")}
    save_stage2_results(
        "2026-10-07T12:00:00", results=results, ranked=[("CENT", results["CENT"]), ("SCSC", results["SCSC"])],
        stage1_run_timestamp=STAGE1_TS, prefilter_run_timestamp=PREFILTER_TS, as_of="2026-09-24", config_hash="x",
        peer_base_size=9, simfin_ids={}, db_path=stage2_db, rule_state=None,
    )
    code, out = world.run(stage2_results_db_path=stage2_db, stage2_run_timestamp="2026-10-07T12:00:00")
    assert code == 0, out
    assert "von 2 Stage-2-Bestandenen" in out and "2 Firmen, davon 2 in diesem Lauf" in out


def test_a_caller_side_input_error_aborts_before_anything_is_written(tmp_path, monkeypatch):
    from metrics_layer.result import MetricInputError

    def boom(*args, **kwargs):
        raise MetricInputError("duplicate fiscal_year entries in periods")

    world = TrendWorld(tmp_path)
    monkeypatch.setattr(run_trend_guard, "evaluate_trend", boom)
    code, _ = world.run()
    assert code == run_trend_guard.EXIT_TECHNICAL_FAILURE and nothing_written(world)


# ---------------------------------------------------------------- rule state (docs/decisions.md "Regelstand und Kurs je Lauf")


def _trend_argv(world):
    return [
        "--db-path", str(world.source_db), "--archive-db-path", str(world.archive_db), "--csv-dir", str(world.csv_dir),
        "--fundamentals-cache-dir", str(world.cache_root), "--stage1-run-timestamp", STAGE1_TS, "--run-timestamp", TREND_TS,
    ]


def test_the_run_archives_its_rule_state_with_the_four_rule_files(tmp_path):
    from store_layer.rule_state import RULE_FILES

    world = TrendWorld(tmp_path)
    assert run_trend_guard.main(_trend_argv(world)) == 0
    assert world.trend("SELECT step, run_timestamp FROM run_rule_state") == [("trend", TREND_TS)]
    assert {r[0] for r in world.trend("SELECT path FROM run_rule_files")} == set(RULE_FILES)


def test_a_missing_rule_file_stops_the_trend_run_and_writes_nothing(monkeypatch, tmp_path):
    from store_layer.rule_state import RuleStateError

    def broken(**kwargs):
        raise RuleStateError("rule file config/stage2.yaml not readable")

    monkeypatch.setattr(run_trend_guard, "capture_rule_state", broken)
    world = TrendWorld(tmp_path)
    assert run_trend_guard.main(_trend_argv(world)) == 1
    assert nothing_written(world)

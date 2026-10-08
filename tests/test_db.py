import csv
import datetime
import sqlite3

import pytest

from conftest import DEFAULT_THRESHOLDS, all_company_ids, load_company
from metrics_layer.cash_conversion import cash_conversion_3yr_average, net_income_margin_3yr_average
from metrics_layer.net_debt_ebitda import net_debt_ebitda_latest
from metrics_layer.result import MetricResult
from metrics_layer.roic import roic_3yr_average
from gates_layer.stage1_gates import (
    GateResult,
    UNSUPPORTED_BUSINESS_MODEL,
    evaluate_stage1_gates,
    rank_companies,
    unsupported_business_model_result,
)
from store_layer.db import export_survivors_csv, init_db, save_run_results


def _simulated_run_gate_results():
    """Gate results for all fixtures - the simulated run's input, per Issue #6."""
    gate_results = {}
    for company_id in all_company_ids():
        periods = load_company(company_id)["periods"]
        gate_results[company_id] = evaluate_stage1_gates(
            roic=roic_3yr_average(periods, thresholds=DEFAULT_THRESHOLDS.roic),
            net_debt_ebitda=net_debt_ebitda_latest(periods),
            cash_conversion=cash_conversion_3yr_average(periods),
            net_income_margin=net_income_margin_3yr_average(periods),
            thresholds=DEFAULT_THRESHOLDS,
        )
    return gate_results


def _zero_survivor_gate_results():
    """Synthetic gate_results where every company fails - rank_companies() empty."""
    failing = evaluate_stage1_gates(
        roic=MetricResult(value=0.01),
        net_debt_ebitda=MetricResult(value=5.0),
        cash_conversion=MetricResult(value=0.1),
        net_income_margin=MetricResult(value=0.01),
        thresholds=DEFAULT_THRESHOLDS,
    )
    assert failing.status == "failed"
    return {ticker: failing for ticker in ("ZERO01", "ZERO02", "ZERO03")}


def _zero_eliminated_gate_results():
    """Synthetic gate_results where every company passes cleanly - eliminated_results empty."""
    passing = evaluate_stage1_gates(
        roic=MetricResult(value=0.15),
        net_debt_ebitda=MetricResult(value=1.0),
        cash_conversion=MetricResult(value=1.5),
        net_income_margin=MetricResult(value=0.05),
        thresholds=DEFAULT_THRESHOLDS,
    )
    assert passing.status == "passed"
    return {ticker: passing for ticker in ("CLEAN01", "CLEAN02")}


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


def _query_eliminated(db_path, run_timestamp):
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM eliminated_results WHERE run_timestamp = ? ORDER BY ticker",
            (run_timestamp,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "archive.db"
    init_db(db_path)
    init_db(db_path)  # must not raise on a second call

    connection = sqlite3.connect(db_path)
    try:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('run_results', 'eliminated_results', 'runs')"
        ).fetchall()
    finally:
        connection.close()
    # each table must exist exactly once - catches a regression that makes
    # either CREATE TABLE non-idempotent, not just "some table exists"
    table_names = [row[0] for row in tables]
    assert table_names.count("run_results") == 1
    assert table_names.count("eliminated_results") == 1
    assert table_names.count("runs") == 1


def test_save_run_results_only_survivors_are_archived(tmp_path):
    db_path = tmp_path / "archive.db"
    gate_results = _simulated_run_gate_results()
    run_timestamp = "2026-09-15T14:30:00"

    save_run_results(run_timestamp, gate_results, config_hash=None, db_path=db_path, rule_state=None)

    rows = _query_run(db_path, run_timestamp)
    archived_tickers = {row["ticker"] for row in rows}

    expected_survivors = {ticker for ticker, _ in rank_companies(gate_results)}
    assert archived_tickers == expected_survivors

    # sanity: a failed and a not_evaluable company from the fixture set must
    # be excluded, not just "some" tickers missing
    assert gate_results["GEKO12"].status == "failed"
    assert gate_results["ORBT18"].status == "not_evaluable"
    assert "GEKO12" not in archived_tickers
    assert "ORBT18" not in archived_tickers
    # NAKA17 is failed with an invalid_reason (docs/decisions.md, "Phase 2.0d") -
    # still not a survivor
    assert gate_results["NAKA17"].status == "failed"
    assert "NAKA17" not in archived_tickers


def test_save_run_results_full_funnel_partitions_every_company_exactly_once(tmp_path):
    # Every fixture (currently 31 - the whole simulated run's universe) must
    # land in exactly one of run_results/eliminated_results: never both,
    # never neither.
    db_path = tmp_path / "archive.db"
    gate_results = _simulated_run_gate_results()
    run_timestamp = "2026-09-21T09:00:00"

    save_run_results(run_timestamp, gate_results, config_hash=None, db_path=db_path, rule_state=None)

    survivor_tickers = {row["ticker"] for row in _query_run(db_path, run_timestamp)}
    eliminated_tickers = {row["ticker"] for row in _query_eliminated(db_path, run_timestamp)}

    assert survivor_tickers & eliminated_tickers == set()
    assert survivor_tickers | eliminated_tickers == set(gate_results.keys())
    assert len(survivor_tickers) + len(eliminated_tickers) == len(gate_results)

    expected_survivors = {ticker for ticker, _ in rank_companies(gate_results)}
    expected_eliminated = {
        ticker for ticker, result in gate_results.items() if result.status in ("failed", "not_evaluable")
    }
    assert survivor_tickers == expected_survivors
    assert eliminated_tickers == expected_eliminated


def test_save_run_results_unrecognized_status_raises_instead_of_vanishing(tmp_path):
    # Review-Fund: _survivor_rows() and _eliminated_rows() each check their
    # own fixed status set (PASSED/PASSED_WITH_FLAGS vs. FAILED/
    # NOT_EVALUABLE). Those only exhaustively partition every company
    # because exactly 4 status strings exist today - status is a plain
    # string, not an enum (gates_layer-Entscheidung 5). A GateResult with
    # any other status must raise, not silently land in neither table.
    db_path = tmp_path / "archive.db"
    bogus = GateResult(
        status="pending",
        flags=(),
        invalid_reasons=(),
        roic=0.15,
        net_debt_ebitda=1.0,
        cash_conversion=1.5,
        net_income_margin=0.05,
    )
    gate_results = {"BOGUS01": bogus}

    with pytest.raises(ValueError, match="BOGUS01"):
        save_run_results("2026-09-21T09:00:00", gate_results, config_hash=None, db_path=db_path, rule_state=None)


def test_save_run_results_eliminated_rows_have_expected_columns(tmp_path):
    db_path = tmp_path / "archive.db"
    gate_results = _simulated_run_gate_results()
    run_timestamp = "2026-09-21T09:00:00"

    save_run_results(run_timestamp, gate_results, config_hash=None, db_path=db_path, rule_state=None)
    eliminated_by_ticker = {row["ticker"]: row for row in _query_eliminated(db_path, run_timestamp)}

    # ORBT18: not_evaluable (insufficient_years, no valid hard fail) - its
    # invalid_reasons is populated.
    assert gate_results["ORBT18"].status == "not_evaluable"
    orbt18_row = eliminated_by_ticker["ORBT18"]
    assert orbt18_row["status"] == "not_evaluable"
    assert "insufficient_years" in orbt18_row["invalid_reasons"]

    # NAKA17: failed although ic_non_positive makes roic invalid - Net Debt/EBITDA
    # is valid and in its hard-fail zone (docs/decisions.md, "Phase 2.0d"). roic
    # stays None and the invalid_reasons are kept, so the data gap remains countable.
    assert gate_results["NAKA17"].status == "failed"
    naka17_row = eliminated_by_ticker["NAKA17"]
    assert naka17_row["status"] == "failed"
    assert naka17_row["roic"] is None
    assert naka17_row["net_debt_ebitda"] is not None
    assert "ic_non_positive" in naka17_row["invalid_reasons"]
    assert naka17_row["invalid_reasons"] != ""

    # GEKO12: failed on the Net Debt/EBITDA hard threshold alone - all four
    # metrics are valid, so all four raw values are populated and
    # invalid_reasons stays empty.
    assert gate_results["GEKO12"].status == "failed"
    geko12_row = eliminated_by_ticker["GEKO12"]
    assert geko12_row["status"] == "failed"
    assert geko12_row["roic"] is not None
    assert geko12_row["net_debt_ebitda"] is not None
    assert geko12_row["cash_conversion"] is not None
    assert geko12_row["net_income_margin"] is not None
    assert geko12_row["invalid_reasons"] == ""


def test_failed_with_invalid_reasons_is_archived_as_eliminated_and_countable_as_data_gap(tmp_path):
    # docs/decisions.md, "Phase 2.0d", Entscheidungen 2/3: a failed company that
    # also has an invalid metric keeps its invalid_reasons in eliminated_results,
    # never becomes a survivor, and is countable separately from not_evaluable.
    failed_with_gap = evaluate_stage1_gates(
        roic=MetricResult(value=0.05),
        net_debt_ebitda=MetricResult(value=None, invalid_reason="debt_data_completely_missing"),
        cash_conversion=MetricResult(value=1.5),
        net_income_margin=MetricResult(value=0.05),
        thresholds=DEFAULT_THRESHOLDS,
    )
    plain_failed = evaluate_stage1_gates(
        roic=MetricResult(value=0.05),
        net_debt_ebitda=MetricResult(value=1.0),
        cash_conversion=MetricResult(value=1.5),
        net_income_margin=MetricResult(value=0.05),
        thresholds=DEFAULT_THRESHOLDS,
    )
    not_evaluable = evaluate_stage1_gates(
        roic=MetricResult(value=0.15),
        net_debt_ebitda=MetricResult(value=None, invalid_reason="debt_data_completely_missing"),
        cash_conversion=MetricResult(value=1.5),
        net_income_margin=MetricResult(value=0.05),
        thresholds=DEFAULT_THRESHOLDS,
    )
    clean = evaluate_stage1_gates(
        roic=MetricResult(value=0.15),
        net_debt_ebitda=MetricResult(value=1.0),
        cash_conversion=MetricResult(value=1.5),
        net_income_margin=MetricResult(value=0.05),
        thresholds=DEFAULT_THRESHOLDS,
    )
    assert (failed_with_gap.status, plain_failed.status, not_evaluable.status) == ("failed", "failed", "not_evaluable")
    gate_results = {"GAP01": failed_with_gap, "FAIL01": plain_failed, "NOEV01": not_evaluable, "CLEAN01": clean}
    db_path = tmp_path / "archive.db"
    run_timestamp = "2026-09-30T10:00:00"

    save_run_results(run_timestamp, gate_results, config_hash=None, db_path=db_path, rule_state=None)

    assert [ticker for ticker, _ in rank_companies(gate_results)] == ["CLEAN01"]
    assert {row["ticker"] for row in _query_run(db_path, run_timestamp)} == {"CLEAN01"}
    eliminated = {row["ticker"]: row for row in _query_eliminated(db_path, run_timestamp)}
    assert set(eliminated) == {"GAP01", "FAIL01", "NOEV01"}
    assert eliminated["GAP01"]["status"] == "failed"
    assert eliminated["GAP01"]["invalid_reasons"] == "debt_data_completely_missing"
    assert eliminated["GAP01"]["net_debt_ebitda"] is None
    assert eliminated["GAP01"]["roic"] == 0.05
    assert eliminated["FAIL01"]["invalid_reasons"] == ""

    connection = sqlite3.connect(db_path)
    try:
        gap_count = connection.execute(
            "SELECT COUNT(*) FROM eliminated_results WHERE status = 'failed' AND invalid_reasons != ''"
        ).fetchone()[0]
    finally:
        connection.close()
    assert gap_count == 1


def test_zero_eliminated_run_writes_empty_eliminated_table(tmp_path):
    db_path = tmp_path / "archive.db"
    gate_results = _zero_eliminated_gate_results()
    run_timestamp = "2026-09-15T08:00:00"

    save_run_results(run_timestamp, gate_results, config_hash=None, db_path=db_path, rule_state=None)

    assert _query_eliminated(db_path, run_timestamp) == []
    run_row = _query_runs_table(db_path, run_timestamp)
    assert run_row["survivors_count"] == len(gate_results)
    assert len(_query_run(db_path, run_timestamp)) == len(gate_results)


def test_save_run_results_rank_position_matches_rank_companies(tmp_path):
    db_path = tmp_path / "archive.db"
    gate_results = _simulated_run_gate_results()
    run_timestamp = "2026-09-15T14:30:00"

    save_run_results(run_timestamp, gate_results, config_hash=None, db_path=db_path, rule_state=None)

    rows = _query_run(db_path, run_timestamp)
    rank_by_ticker = {row["ticker"]: row["rank_position"] for row in rows}

    expected = {ticker: position for position, (ticker, _) in enumerate(rank_companies(gate_results), start=1)}
    assert rank_by_ticker == expected
    # positions are a contiguous 1..N sequence, not reset per flag-count group
    assert sorted(rank_by_ticker.values()) == list(range(1, len(rank_by_ticker) + 1))


def test_save_run_results_two_timestamps_do_not_overwrite_each_other(tmp_path):
    db_path = tmp_path / "archive.db"
    gate_results = _simulated_run_gate_results()

    save_run_results("2026-09-15T14:30:00", gate_results, config_hash=None, db_path=db_path, rule_state=None)
    save_run_results("2026-09-16T09:00:00", gate_results, config_hash=None, db_path=db_path, rule_state=None)

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

    save_run_results(run_timestamp, gate_results, config_hash=None, db_path=db_path, rule_state=None)

    # a forward-log is a historical record: re-saving the same run_timestamp
    # is a caller bug that must raise (composite primary key violation),
    # never silently overwrite the already-archived run
    with pytest.raises(sqlite3.IntegrityError):
        save_run_results(run_timestamp, gate_results, config_hash=None, db_path=db_path, rule_state=None)

    # the original rows must be untouched by the failed second attempt, in
    # BOTH run_results and eliminated_results (single shared transaction)
    rows = _query_run(db_path, run_timestamp)
    assert len(rows) == len(rank_companies(gate_results))
    eliminated_rows = _query_eliminated(db_path, run_timestamp)
    expected_eliminated_count = sum(
        1 for result in gate_results.values() if result.status in ("failed", "not_evaluable")
    )
    assert len(eliminated_rows) == expected_eliminated_count


def test_export_survivors_csv_matches_db_for_same_run(tmp_path):
    db_path = tmp_path / "archive.db"
    csv_dir = tmp_path / "runs"
    gate_results = _simulated_run_gate_results()
    run_timestamp = "2026-09-15T14:30:00"

    save_run_results(run_timestamp, gate_results, config_hash=None, db_path=db_path, rule_state=None)
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
    kwargs = (
        {"config_hash": None, "db_path": tmp_path / "archive.db"}
        if func_name == "save_run_results"
        else {"output_dir": tmp_path / "runs"}
    )

    with pytest.raises(TypeError):
        func(run_timestamp, gate_results, **kwargs)


def test_zero_survivor_run_still_writes_runs_row_and_header_only_csv(tmp_path):
    db_path = tmp_path / "archive.db"
    csv_dir = tmp_path / "runs"
    gate_results = _zero_survivor_gate_results()
    run_timestamp = "2026-09-15T08:00:00"
    assert rank_companies(gate_results) == []  # sanity: genuinely 0 survivors

    save_run_results(run_timestamp, gate_results, config_hash=None, db_path=db_path, rule_state=None)
    csv_path = export_survivors_csv(run_timestamp, gate_results, output_dir=csv_dir)

    run_row = _query_runs_table(db_path, run_timestamp)
    assert run_row is not None
    assert run_row["companies_evaluated"] == len(gate_results)
    assert run_row["survivors_count"] == 0

    assert _query_run(db_path, run_timestamp) == []

    # every one of the (synthetically all-failed) companies is eliminated
    eliminated_tickers = {row["ticker"] for row in _query_eliminated(db_path, run_timestamp)}
    assert eliminated_tickers == set(gate_results.keys())

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
            "data_quality_flags",
            "borderline_sector",
        ]
        assert list(reader) == []  # header only, no data rows


def test_zero_survivor_run_and_survivor_run_coexist(tmp_path):
    db_path = tmp_path / "archive.db"
    csv_dir = tmp_path / "runs"

    zero_gate_results = _zero_survivor_gate_results()
    zero_timestamp = "2026-09-15T08:00:00"
    save_run_results(zero_timestamp, zero_gate_results, config_hash=None, db_path=db_path, rule_state=None)
    zero_csv_path = export_survivors_csv(zero_timestamp, zero_gate_results, output_dir=csv_dir)

    real_gate_results = _simulated_run_gate_results()
    real_timestamp = "2026-09-15T14:30:00"
    save_run_results(real_timestamp, real_gate_results, config_hash=None, db_path=db_path, rule_state=None)
    real_csv_path = export_survivors_csv(real_timestamp, real_gate_results, output_dir=csv_dir)

    zero_run_row = _query_runs_table(db_path, zero_timestamp)
    real_run_row = _query_runs_table(db_path, real_timestamp)
    assert zero_run_row["survivors_count"] == 0
    real_survivor_count = len(rank_companies(real_gate_results))
    assert real_survivor_count > 0
    assert real_run_row["survivors_count"] == real_survivor_count

    assert _query_run(db_path, zero_timestamp) == []
    assert len(_query_run(db_path, real_timestamp)) == real_survivor_count

    # the all-failed zero-survivor run's companies are all eliminated; the
    # real run's eliminated count is whatever isn't a survivor - the two
    # timestamps' eliminated_results rows must not bleed into each other
    zero_eliminated = _query_eliminated(db_path, zero_timestamp)
    real_eliminated = _query_eliminated(db_path, real_timestamp)
    assert {row["ticker"] for row in zero_eliminated} == set(zero_gate_results.keys())
    assert len(real_eliminated) == len(real_gate_results) - real_survivor_count

    with open(zero_csv_path, newline="", encoding="utf-8") as f:
        assert list(csv.DictReader(f)) == []
    with open(real_csv_path, newline="", encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == real_survivor_count


# --- data_quality_flags persistence (docs/decisions.md, "net_debt-Zuverlaessigkeit -
# Stock-Flow-Abgleich", Entscheidung 5) -----------------------------------------


_DQ_FLAGS = ("debt_component_missing_defaulted_zero", "pl_lags_other_statements")


def _gate_results_with_data_quality_flags():
    """One survivor with flags, one survivor without, one failed and one not_evaluable, each with its own flags."""
    passing = dict(
        roic=MetricResult(value=0.15),
        net_debt_ebitda=MetricResult(value=1.0),
        cash_conversion=MetricResult(value=1.5),
        net_income_margin=MetricResult(value=0.05),
        thresholds=DEFAULT_THRESHOLDS,
    )
    return {
        "SURV_FLAGGED": evaluate_stage1_gates(**passing, data_quality_flags=_DQ_FLAGS),
        "SURV_CLEAN": evaluate_stage1_gates(**passing),
        "ELIM_FAILED": evaluate_stage1_gates(
            roic=MetricResult(value=0.01),
            net_debt_ebitda=MetricResult(value=5.0),
            cash_conversion=MetricResult(value=0.1),
            net_income_margin=MetricResult(value=0.01),
            thresholds=DEFAULT_THRESHOLDS,
            data_quality_flags=("year_dropped_missing_required_field",),
        ),
        "ELIM_NOT_EVALUABLE": evaluate_stage1_gates(
            roic=MetricResult(value=None, invalid_reason="debt_data_completely_missing"),
            net_debt_ebitda=MetricResult(value=None, invalid_reason="debt_data_completely_missing"),
            cash_conversion=MetricResult(value=1.5),
            net_income_margin=MetricResult(value=0.05),
            thresholds=DEFAULT_THRESHOLDS,
            data_quality_flags=("debt_data_completely_missing",) + _DQ_FLAGS,
        ),
    }


def _table_columns(db_path, table):
    connection = sqlite3.connect(db_path)
    try:
        return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
    finally:
        connection.close()


def test_new_db_has_data_quality_flags_column_in_both_tables(tmp_path):
    db_path = tmp_path / "archive.db"
    init_db(db_path)
    assert _table_columns(db_path, "run_results").count("data_quality_flags") == 1
    assert _table_columns(db_path, "eliminated_results").count("data_quality_flags") == 1


def test_new_db_has_config_hash_column_in_runs_table(tmp_path):
    db_path = tmp_path / "archive.db"
    init_db(db_path)
    assert _table_columns(db_path, "runs").count("config_hash") == 1


def test_save_run_results_persists_config_hash_in_runs_table(tmp_path):
    db_path = tmp_path / "archive.db"
    run_timestamp = "2026-09-23T18:00:00"
    save_run_results(run_timestamp, _zero_eliminated_gate_results(), config_hash="abc123", db_path=db_path, rule_state=None)
    run_row = _query_runs_table(db_path, run_timestamp)
    assert run_row["config_hash"] == "abc123"


def test_save_run_results_persists_data_quality_flags_in_both_tables(tmp_path):
    db_path = tmp_path / "archive.db"
    run_timestamp = "2026-09-21T16:00:00"
    save_run_results(run_timestamp, _gate_results_with_data_quality_flags(), config_hash=None, db_path=db_path, rule_state=None)

    survivors = {row["ticker"]: row for row in _query_run(db_path, run_timestamp)}
    assert survivors["SURV_FLAGGED"]["data_quality_flags"] == ",".join(_DQ_FLAGS)
    assert survivors["SURV_CLEAN"]["data_quality_flags"] == ""

    eliminated = {row["ticker"]: row for row in _query_eliminated(db_path, run_timestamp)}
    assert eliminated["ELIM_FAILED"]["data_quality_flags"] == "year_dropped_missing_required_field"
    assert eliminated["ELIM_NOT_EVALUABLE"]["data_quality_flags"] == "debt_data_completely_missing," + ",".join(
        _DQ_FLAGS
    )


def test_data_quality_flags_never_change_status_or_ranking(tmp_path):
    # Informational only: identical metrics with and without flags rank in the
    # same group (0 metric flags) - only the ticker decides within it.
    results = _gate_results_with_data_quality_flags()
    assert results["SURV_FLAGGED"].status == results["SURV_CLEAN"].status == "passed"
    assert [ticker for ticker, _ in rank_companies(results)] == ["SURV_CLEAN", "SURV_FLAGGED"]


def test_export_survivors_csv_carries_data_quality_flags(tmp_path):
    csv_dir = tmp_path / "runs"
    csv_path = export_survivors_csv("2026-09-21T16:00:00", _gate_results_with_data_quality_flags(), output_dir=csv_dir)
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = {row["ticker"]: row for row in csv.DictReader(f)}
    assert set(rows) == {"SURV_FLAGGED", "SURV_CLEAN"}  # survivors only
    assert rows["SURV_FLAGGED"]["data_quality_flags"] == ",".join(_DQ_FLAGS)
    assert rows["SURV_CLEAN"]["data_quality_flags"] == ""


_OLD_SCHEMA_RUN_RESULTS = """
    CREATE TABLE run_results (
        run_timestamp TEXT NOT NULL, ticker TEXT NOT NULL, roic_3yr REAL NOT NULL,
        net_debt_ebitda REAL NOT NULL, cash_conversion_3yr REAL NOT NULL, status TEXT NOT NULL,
        flags TEXT NOT NULL, rank_position INTEGER NOT NULL, PRIMARY KEY (run_timestamp, ticker))
"""
_OLD_SCHEMA_ELIMINATED = """
    CREATE TABLE eliminated_results (
        run_timestamp TEXT NOT NULL, ticker TEXT NOT NULL, status TEXT NOT NULL, roic REAL,
        net_debt_ebitda REAL, cash_conversion REAL, net_income_margin REAL,
        invalid_reasons TEXT NOT NULL, PRIMARY KEY (run_timestamp, ticker))
"""


def test_init_db_upgrades_an_existing_archive_and_stays_idempotent(tmp_path):
    # An archive created before the column existed: init_db() must add it via
    # ALTER TABLE (old rows read back as ''), not fail, and not add it twice.
    db_path = tmp_path / "old_archive.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(_OLD_SCHEMA_RUN_RESULTS)
        connection.execute(_OLD_SCHEMA_ELIMINATED)
        connection.execute("INSERT INTO run_results VALUES ('2026-09-01T09:00:00', 'OLD1', 0.2, 1.0, 1.1, 'passed', '', 1)")
        connection.execute(
            "INSERT INTO eliminated_results VALUES ('2026-09-01T09:00:00', 'OLD2', 'failed', 0.1, 3.0, 0.9, 0.05, '')"
        )
        connection.commit()
    finally:
        connection.close()
    assert "data_quality_flags" not in _table_columns(db_path, "run_results")

    init_db(db_path)
    init_db(db_path)  # a second call must be a no-op, not "duplicate column name"

    assert _table_columns(db_path, "run_results").count("data_quality_flags") == 1
    assert _table_columns(db_path, "eliminated_results").count("data_quality_flags") == 1
    assert _query_run(db_path, "2026-09-01T09:00:00")[0]["data_quality_flags"] == ""
    assert _query_eliminated(db_path, "2026-09-01T09:00:00")[0]["data_quality_flags"] == ""

    # and a new run on the upgraded archive persists flags next to the old rows
    save_run_results("2026-09-21T16:00:00", _gate_results_with_data_quality_flags(), config_hash=None, db_path=db_path, rule_state=None)
    new_rows = {row["ticker"]: row for row in _query_run(db_path, "2026-09-21T16:00:00")}
    assert new_rows["SURV_FLAGGED"]["data_quality_flags"] == ",".join(_DQ_FLAGS)



# --- business-model profile columns (Issue #51, docs/decisions.md "Phase 2.0e") ------


def _profiled_gate_results():
    """One clean survivor per borderline value, a failed and a not_evaluable company, an unsupported one, and one never profile-checked."""
    import dataclasses

    base = _simulated_run_gate_results()
    return {
        "SURV_PLAIN": dataclasses.replace(base["ACME01"], profile="non_financial", borderline_sector=False),
        "SURV_BORDER": dataclasses.replace(base["APRT11"], profile="non_financial", borderline_sector=True),
        "ELIM_FAILED": dataclasses.replace(base["GEKO12"], profile="non_financial", borderline_sector=True),
        "ELIM_NOEV": dataclasses.replace(base["ORBT18"], profile="non_financial", borderline_sector=False),
        "ELIM_BANK": unsupported_business_model_result("banks"),
        "UNCHECKED": base["DUFF22"],  # profile None / borderline None: pilot-runner style, not checked
    }


def test_unsupported_business_model_is_archived_as_eliminated_and_the_partition_still_holds(tmp_path):
    db_path = tmp_path / "archive.db"
    gate_results = _profiled_gate_results()
    save_run_results("2026-09-30T10:00:00", gate_results, config_hash=None, db_path=db_path, rule_state=None)

    survivors = {row["ticker"] for row in _query_run(db_path, "2026-09-30T10:00:00")}
    eliminated = {row["ticker"]: row for row in _query_eliminated(db_path, "2026-09-30T10:00:00")}
    assert survivors & set(eliminated) == set()
    assert survivors | set(eliminated) == set(gate_results)
    assert eliminated["ELIM_BANK"]["status"] == UNSUPPORTED_BUSINESS_MODEL
    assert eliminated["ELIM_BANK"]["invalid_reasons"] == ""  # not a data gap
    assert eliminated["ELIM_BANK"]["profile"] == "banks"
    assert eliminated["ELIM_BANK"]["roic"] is None and eliminated["ELIM_BANK"]["net_income_margin"] is None
    assert "ELIM_BANK" not in survivors
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT survivors_count, companies_evaluated FROM runs").fetchone() == (
            len(survivors),
            len(gate_results),
        )
    finally:
        connection.close()


def test_an_unrecognized_status_still_raises_after_adding_unsupported_business_model(tmp_path):
    bogus = GateResult(
        status="something_else", flags=(), invalid_reasons=(), roic=None, net_debt_ebitda=None,
        cash_conversion=None, net_income_margin=None,
    )
    with pytest.raises(ValueError, match="BOGUS02"):
        save_run_results("2026-09-30T10:00:00", {"BOGUS02": bogus}, config_hash=None, db_path=tmp_path / "a.db", rule_state=None)


def test_profile_and_borderline_are_persisted_in_both_tables_and_null_when_not_checked(tmp_path):
    db_path = tmp_path / "archive.db"
    save_run_results("2026-09-30T10:00:00", _profiled_gate_results(), config_hash=None, db_path=db_path, rule_state=None)
    survivors = {row["ticker"]: row for row in _query_run(db_path, "2026-09-30T10:00:00")}
    eliminated = {row["ticker"]: row for row in _query_eliminated(db_path, "2026-09-30T10:00:00")}

    assert (survivors["SURV_PLAIN"]["profile"], survivors["SURV_PLAIN"]["borderline_sector"]) == ("non_financial", 0)
    assert (survivors["SURV_BORDER"]["profile"], survivors["SURV_BORDER"]["borderline_sector"]) == ("non_financial", 1)
    assert (eliminated["ELIM_FAILED"]["profile"], eliminated["ELIM_FAILED"]["borderline_sector"]) == ("non_financial", 1)
    assert (eliminated["ELIM_NOEV"]["profile"], eliminated["ELIM_NOEV"]["borderline_sector"]) == ("non_financial", 0)
    assert (eliminated["ELIM_BANK"]["profile"], eliminated["ELIM_BANK"]["borderline_sector"]) == ("banks", 0)
    unchecked = survivors.get("UNCHECKED") or eliminated["UNCHECKED"]
    assert (unchecked["profile"], unchecked["borderline_sector"]) == (None, None)  # NULL = not checked


def test_borderline_marker_does_not_change_survivor_status_flags_or_rank_in_the_archive(tmp_path):
    import dataclasses

    plain = {t: r for t, r in _simulated_run_gate_results().items() if r.status in ("passed", "passed_with_flags")}
    marked = {t: dataclasses.replace(r, profile="non_financial", borderline_sector=True) for t, r in plain.items()}
    db_plain, db_marked = tmp_path / "plain.db", tmp_path / "marked.db"
    save_run_results("2026-09-30T10:00:00", plain, config_hash=None, db_path=db_plain, rule_state=None)
    save_run_results("2026-09-30T10:00:00", marked, config_hash=None, db_path=db_marked, rule_state=None)

    def core(db_path):
        return [
            (r["ticker"], r["status"], r["flags"], r["rank_position"])
            for r in _query_run(db_path, "2026-09-30T10:00:00")
        ]

    assert core(db_plain) == core(db_marked)


def test_survivors_csv_has_borderline_sector_as_the_last_column_empty_for_no(tmp_path):
    gate_results = _profiled_gate_results()
    csv_path = export_survivors_csv("2026-09-30T10:00:00", gate_results, output_dir=tmp_path / "runs")
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames[-1] == "borderline_sector"
        assert reader.fieldnames[:-1] == [
            "rank_position", "ticker", "status", "flags", "roic_3yr", "net_debt_ebitda",
            "cash_conversion_3yr", "data_quality_flags",
        ]  # the earlier columns keep their positions
        rows = {row["ticker"]: row for row in reader}
    assert rows["SURV_BORDER"]["borderline_sector"] == "1"
    assert rows["SURV_PLAIN"]["borderline_sector"] == ""  # empty string = no
    assert rows["UNCHECKED"]["borderline_sector"] == ""  # not checked reads as empty too
    assert "ELIM_BANK" not in rows  # eliminated companies never reach the survivors CSV


def test_init_db_adds_profile_columns_to_an_older_archive_without_touching_rows_and_stays_idempotent(tmp_path):
    db_path = tmp_path / "old_archive.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(_OLD_SCHEMA_RUN_RESULTS)
        connection.execute(_OLD_SCHEMA_ELIMINATED)
        connection.execute("INSERT INTO run_results VALUES ('2026-09-01T09:00:00', 'OLD1', 0.2, 1.0, 1.1, 'passed', '', 1)")
        connection.execute(
            "INSERT INTO eliminated_results VALUES ('2026-09-01T09:00:00', 'OLD2', 'failed', 0.1, 3.0, 0.9, 0.05, '')"
        )
        connection.commit()
    finally:
        connection.close()

    def snapshot():
        connection = sqlite3.connect(db_path)
        try:
            return {
                table: connection.execute(
                    f"SELECT run_timestamp, ticker, status FROM {table} ORDER BY ticker"
                ).fetchall()
                for table in ("run_results", "eliminated_results")
            }
        finally:
            connection.close()

    before = snapshot()
    for table in ("run_results", "eliminated_results"):
        assert "profile" not in _table_columns(db_path, table)

    init_db(db_path)
    init_db(db_path)  # idempotent

    for table in ("run_results", "eliminated_results"):
        columns = _table_columns(db_path, table)
        assert columns.count("profile") == 1 and columns.count("borderline_sector") == 1
    assert snapshot() == before  # same rows, same old values
    assert _query_run(db_path, "2026-09-01T09:00:00")[0]["profile"] is None  # NULL: not recorded then
    assert _query_eliminated(db_path, "2026-09-01T09:00:00")[0]["borderline_sector"] is None

    # a new run on the migrated archive records its profile next to the old rows
    save_run_results("2026-09-30T10:00:00", _profiled_gate_results(), config_hash=None, db_path=db_path, rule_state=None)
    assert {r["ticker"]: r["profile"] for r in _query_eliminated(db_path, "2026-09-30T10:00:00")}["ELIM_BANK"] == "banks"


_OLD_SCHEMA_RUNS = """
    CREATE TABLE runs (
        run_timestamp TEXT PRIMARY KEY, companies_evaluated INTEGER NOT NULL,
        survivors_count INTEGER NOT NULL)
"""


def test_init_db_upgrades_an_existing_runs_table_and_stays_idempotent(tmp_path):
    # An archive whose runs table predates config_hash: init_db() must add it
    # via ALTER TABLE, not fail, and not add it twice. Unlike
    # data_quality_flags's '' sentinel, old rows must read back NULL - a
    # config_hash was genuinely never recorded for them, not "recorded as
    # empty" (docs/decisions.md, "Schwellen nach config/thresholds.yaml").
    db_path = tmp_path / "old_archive.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(_OLD_SCHEMA_RUNS)
        connection.execute("INSERT INTO runs VALUES ('2026-09-01T09:00:00', 3, 1)")
        connection.commit()
    finally:
        connection.close()
    assert "config_hash" not in _table_columns(db_path, "runs")

    init_db(db_path)
    init_db(db_path)  # a second call must be a no-op, not "duplicate column name"

    assert _table_columns(db_path, "runs").count("config_hash") == 1
    old_row = _query_runs_table(db_path, "2026-09-01T09:00:00")
    assert old_row["config_hash"] is None

    # a new run on the upgraded archive persists its own config_hash next to the old, NULL row
    save_run_results(
        "2026-09-23T18:00:00", _zero_eliminated_gate_results(), config_hash="def456", db_path=db_path, rule_state=None
    )
    new_row = _query_runs_table(db_path, "2026-09-23T18:00:00")
    assert new_row["config_hash"] == "def456"


# --- universe pre-filter archive (Issue #39) ----------------------------------

from gates_layer import universe_prefilter as _pf  # noqa: E402
from store_layer.db import export_universe_survivors, save_universe_prefilter  # noqa: E402

_PF_TS = "2026-09-24T14:30:00"


def _pf_result(
    simfin_id, ticker="ABC", *, status=_pf.PASSED, reason=None, cap=2e8, median=1e7, rows=60, price_date="default", sector=106005
):
    if price_date == "default":  # None must stay expressible: a company with no price date at all
        price_date = datetime.date(2026, 9, 23)
    return _pf.PrefilterResult(simfin_id, ticker, sector, status, reason, cap, median, rows, price_date)


def _pf_mixed_results():
    return [
        _pf_result(1, "AAA"),
        _pf_result(2, "BBB", status=_pf.EXCLUDED, reason=_pf.MARKET_CAP_ABOVE_MAX, cap=2e10, median=5e7),
        _pf_result(3, "CCC", status=_pf.EXCLUDED, reason=_pf.NO_SHARES, cap=None, median=3e6),
        _pf_result(
            4, None, status=_pf.EXCLUDED, reason=_pf.NO_CLASSIFICATION, cap=None, median=None, rows=None, price_date=None, sector=None
        ),
        _pf_result(5, "EEE", status=_pf.EXCLUDED, reason=_pf.NOT_FETCHED, cap=None, median=None, rows=None, price_date=None),
    ]


def _query_prefilter(db_path, run_timestamp=_PF_TS):
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        return {
            row["simfin_id"]: dict(row)
            for row in connection.execute("SELECT * FROM universe_prefilter WHERE run_timestamp = ?", (run_timestamp,))
        }
    finally:
        connection.close()


def test_init_db_creates_the_universe_prefilter_table_idempotently(tmp_path):
    db_path = tmp_path / "archive.db"
    init_db(db_path)
    init_db(db_path)
    assert _table_columns(db_path, "universe_prefilter") == [
        "run_timestamp",
        "simfin_id",
        "ticker",
        "sector_code",
        "market_cap_usd",
        "median_dollar_volume_usd",
        "median_window_rows",
        "price_date",
        "status",
        "reason",
        "config_hash",
        "close_price_usd",  # added by docs/decisions.md "Regelstand und Kurs je Lauf" (Punkt 4)
        "shares_outstanding",
    ]


def test_an_archive_from_before_the_table_gets_it_without_touching_old_data(tmp_path):
    db_path = tmp_path / "old_archive.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(_OLD_SCHEMA_RUNS)
        connection.execute("INSERT INTO runs VALUES ('2026-09-01T09:00:00', 3, 1)")
        connection.commit()
    finally:
        connection.close()
    assert _table_columns(db_path, "universe_prefilter") == []  # PRAGMA on a missing table is empty

    init_db(db_path)
    assert "config_hash" in _table_columns(db_path, "universe_prefilter")
    assert _query_runs_table(db_path, "2026-09-01T09:00:00")["companies_evaluated"] == 3
    save_universe_prefilter(_PF_TS, _pf_mixed_results(), config_hash="abc123", db_path=db_path, rule_state=None)
    assert len(_query_prefilter(db_path)) == 5


def test_save_universe_prefilter_archives_passed_and_excluded_with_their_values(tmp_path):
    db_path = tmp_path / "archive.db"
    save_universe_prefilter(_PF_TS, _pf_mixed_results(), config_hash="abc123", db_path=db_path, rule_state=None)
    rows = _query_prefilter(db_path)
    assert set(rows) == {1, 2, 3, 4, 5}
    assert rows[1]["status"] == "passed" and rows[1]["reason"] is None
    assert rows[1]["price_date"] == "2026-09-23" and rows[1]["median_window_rows"] == 60
    # an excluded company keeps its computed values - the point of archiving them
    assert (rows[2]["status"], rows[2]["reason"]) == ("excluded", "market_cap_above_max")
    assert rows[2]["market_cap_usd"] == 2e10 and rows[2]["median_dollar_volume_usd"] == 5e7
    assert rows[3]["market_cap_usd"] is None and rows[3]["median_dollar_volume_usd"] == 3e6
    # no ticker / no classification / nothing computed is stored as NULL, not dropped
    assert rows[4]["ticker"] is None and rows[4]["sector_code"] is None and rows[4]["price_date"] is None
    assert rows[5]["reason"] == "not_fetched" and rows[5]["market_cap_usd"] is None
    assert {row["config_hash"] for row in rows.values()} == {"abc123"}
    assert rows[1]["sector_code"] == 106005 and rows[1]["ticker"] == "AAA"


def test_save_universe_prefilter_never_overwrites_and_leaves_no_partial_run(tmp_path):
    db_path = tmp_path / "archive.db"
    save_universe_prefilter(_PF_TS, _pf_mixed_results(), config_hash="abc123", db_path=db_path, rule_state=None)
    with pytest.raises(sqlite3.IntegrityError):
        save_universe_prefilter(_PF_TS, [_pf_result(9, "NEW"), _pf_result(1, "AAA")], config_hash="other", db_path=db_path, rule_state=None)
    rows = _query_prefilter(db_path)
    assert set(rows) == {1, 2, 3, 4, 5}  # the new company 9 was rolled back with the failing insert
    assert {row["config_hash"] for row in rows.values()} == {"abc123"}
    # a later run under a new timestamp is fine, and both runs coexist
    save_universe_prefilter("2026-09-25T14:30:00", [_pf_result(1, "AAA")], config_hash="def456", db_path=db_path, rule_state=None)
    assert len(_query_prefilter(db_path, "2026-09-25T14:30:00")) == 1 and len(_query_prefilter(db_path)) == 5


@pytest.mark.parametrize(
    "results,message",
    [
        ([], "no pre-filter results"),
        ([_pf_result(1), _pf_result(1, "DUP")], "appears twice"),
        ([_pf_result(1, status="maybe")], "unrecognized status"),
        ([_pf_result(1, reason=_pf.PRICE_STALE)], "carries reason"),
        ([_pf_result(1, status=_pf.EXCLUDED, reason=None)], "unrecognized reason"),
        ([_pf_result(1, status=_pf.EXCLUDED, reason="made_up")], "unrecognized reason"),
    ],
    ids=["empty", "duplicate_id", "bad_status", "reason_on_passed", "excluded_without_reason", "unknown_reason"],
)
def test_save_universe_prefilter_refuses_inconsistent_input_and_writes_nothing(tmp_path, results, message):
    db_path = tmp_path / "archive.db"
    with pytest.raises(ValueError, match=message):
        save_universe_prefilter(_PF_TS, results, config_hash="abc123", db_path=db_path, rule_state=None)
    assert not db_path.exists()  # validation runs before the archive is even opened


@pytest.mark.parametrize("bad_hash", [None, "", 123])
def test_save_universe_prefilter_requires_a_real_config_hash(tmp_path, bad_hash):
    with pytest.raises(ValueError, match="config_hash"):
        save_universe_prefilter(_PF_TS, [_pf_result(1)], config_hash=bad_hash, db_path=tmp_path / "a.db", rule_state=None)


def test_save_universe_prefilter_requires_a_string_timestamp(tmp_path):
    with pytest.raises(TypeError):
        save_universe_prefilter(datetime.datetime(2026, 9, 24), [_pf_result(1)], config_hash="h", db_path=tmp_path / "a.db", rule_state=None)


def test_export_universe_survivors_lists_only_passed_tickers_sorted_one_per_line(tmp_path):
    results = _pf_mixed_results() + [_pf_result(6, "bbb2"), _pf_result(7, "AAB")]
    path = export_universe_survivors(_PF_TS, results, output_dir=tmp_path / "universe")
    assert path.name == "survivors_2026-09-24T143000.txt"
    assert path.read_bytes() == b"AAA\nAAB\nbbb2\n"  # LF, sorted case-insensitively, no excluded ticker
    assert not list(path.parent.glob("*.tmp"))


def test_export_universe_survivors_writes_an_empty_file_for_zero_survivors(tmp_path):
    only_excluded = [_pf_result(1, "AAA", status=_pf.EXCLUDED, reason=_pf.PRICE_STALE)]
    path = export_universe_survivors(_PF_TS, only_excluded, output_dir=tmp_path)
    assert path.exists() and path.read_bytes() == b""


@pytest.mark.parametrize(
    "results,message",
    [
        ([_pf_result(1, "AAA"), _pf_result(2, "aaa")], "appears twice"),
        ([_pf_result(1, None)], "no ticker"),
    ],
    ids=["duplicate_ticker", "passed_without_ticker"],
)
def test_export_universe_survivors_refuses_an_ambiguous_list(tmp_path, results, message):
    with pytest.raises(ValueError, match=message):
        export_universe_survivors(_PF_TS, results, output_dir=tmp_path)
    assert not list(tmp_path.glob("survivors_*"))


def test_export_universe_survivors_never_touches_config_universe_txt(tmp_path):
    from pathlib import Path

    universe_txt = Path(__file__).resolve().parent.parent / "config" / "universe.txt"
    before = universe_txt.read_bytes()
    export_universe_survivors(_PF_TS, _pf_mixed_results(), output_dir=tmp_path)
    assert universe_txt.read_bytes() == before


def test_a_delisted_marker_row_is_archived_with_no_values(tmp_path):
    db_path = tmp_path / "archive.db"
    marked = _pf_result(
        6, "OLD_old", status=_pf.EXCLUDED, reason=_pf.DELISTED_MARKER, cap=None, median=None, rows=None, price_date=None
    )
    save_universe_prefilter(_PF_TS, [_pf_result(1, "AAA"), marked], config_hash="abc123", db_path=db_path, rule_state=None)
    row = _query_prefilter(db_path)[6]
    assert (row["status"], row["reason"]) == ("excluded", "delisted_marker")
    assert row["market_cap_usd"] is None and row["median_dollar_volume_usd"] is None and row["price_date"] is None

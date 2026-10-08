"""End-to-end tests for run_stage1.py (Issue #9) against the committed
tests/fixtures/simfin_raw/*.json - offline and deterministic, like the rest
of the suite: urllib.request.urlopen is monkeypatched (same pattern as
tests/test_simfin_client.py) so no live SimFin call is ever made.
"""

import io
import json
import sqlite3
from pathlib import Path

import pytest

from conftest import DEFAULT_THRESHOLDS, FakeUrlopenResponse
from data_layer import simfin_client
from metrics_layer.result import MetricInputError

import run_stage1

SIMFIN_RAW_DIR = Path(__file__).parent / "fixtures" / "simfin_raw"


def _load_raw(ticker):
    with open(SIMFIN_RAW_DIR / f"{ticker}.json", encoding="utf-8") as f:
        return json.load(f)


def _install_fake_simfin(monkeypatch, raw_by_ticker):
    """raw_by_ticker: {TICKER: raw_company_dict}. Serves companies/list from
    each raw dict's own id/ticker, and fetch_statements_batch by id - built
    entirely from already-fetched, committed fixture data.
    """
    companies_list = [{"id": raw["id"], "ticker": raw["ticker"]} for raw in raw_by_ticker.values()]
    by_id = {raw["id"]: raw for raw in raw_by_ticker.values()}

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        if url == simfin_client.SIMFIN_COMPANIES_LIST_URL:
            return FakeUrlopenResponse(json.dumps(companies_list).encode())
        assert url.startswith(simfin_client.SIMFIN_STATEMENTS_URL)
        query = url.split("?", 1)[1]
        params = dict(part.split("=", 1) for part in query.split("&"))
        ids = [int(i) for i in params["id"].split(",")]
        return FakeUrlopenResponse(json.dumps([by_id[i] for i in ids if i in by_id]).encode())

    monkeypatch.setattr(simfin_client.urllib.request, "urlopen", fake_urlopen)


def _read_table(db_path, table):
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        return {row["ticker"]: dict(row) for row in connection.execute(f"SELECT * FROM {table}")}
    finally:
        connection.close()


def test_run_stage1_end_to_end_against_the_23_pilot_fixtures(monkeypatch, tmp_path):
    # Uses the real config/universe.txt (all 23 pilot tickers) - only
    # db_path/csv_dir are redirected to tmp_path, network is mocked.
    tickers = run_stage1.load_universe(run_stage1.DEFAULT_UNIVERSE_PATH)
    assert len(tickers) == 23
    raw_by_ticker = {ticker: _load_raw(ticker) for ticker in tickers}
    _install_fake_simfin(monkeypatch, raw_by_ticker)

    db_path = tmp_path / "run_archive.db"
    csv_dir = tmp_path / "runs"
    out = io.StringIO()

    exit_code = run_stage1.run(
        universe_path=run_stage1.DEFAULT_UNIVERSE_PATH,
        db_path=db_path,
        csv_dir=csv_dir,
        api_key="fake-key",
        run_timestamp="2026-09-22T09:00:00",
        out=out,
    )

    assert exit_code == run_stage1.EXIT_OK
    survivors = _read_table(db_path, "run_results")
    eliminated = _read_table(db_path, "eliminated_results")
    assert set(survivors) | set(eliminated) == set(tickers)
    assert not set(survivors) & set(eliminated)

    runs_row = sqlite3.connect(db_path).execute(
        "SELECT companies_evaluated, survivors_count FROM runs WHERE run_timestamp = ?",
        ("2026-09-22T09:00:00",),
    ).fetchone()
    assert runs_row == (23, len(survivors))

    csv_path = csv_dir / "survivors_2026-09-22T090000.csv"
    assert csv_path.exists()

    output = out.getvalue()
    assert "23 companies:" in output
    assert f"run_timestamp=2026-09-22T09:00:00" in output


def test_run_stage1_one_broken_ticker_does_not_abort_the_rest(monkeypatch, tmp_path):
    # Three good, real tickers plus one synthetic, unmappable entry (no PL
    # statement at all - SimFinMappingError) injected among them. The run
    # must still complete (exit 0), the good tickers must still be
    # evaluated normally, and the broken one must land in eliminated_results
    # with a runner-level invalid_reason - not abort the whole run
    # (docs/decisions.md, Issue #9 Entscheidung 2).
    good_tickers = ["UFPT", "USNA", "ATRO"]
    raw_by_ticker = {ticker: _load_raw(ticker) for ticker in good_tickers}
    raw_by_ticker["BROKEN"] = {
        "id": 999999,
        "ticker": "BROKEN",
        "currency": "USD",
        "statements": [],  # no PL statement -> SimFinMappingError in map_simfin_to_periods
    }
    _install_fake_simfin(monkeypatch, raw_by_ticker)

    universe_path = tmp_path / "universe.txt"
    universe_path.write_text("\n".join(["UFPT", "BROKEN", "USNA", "ATRO"]) + "\n", encoding="utf-8")

    db_path = tmp_path / "run_archive.db"
    csv_dir = tmp_path / "runs"
    out = io.StringIO()

    exit_code = run_stage1.run(
        universe_path=universe_path,
        db_path=db_path,
        csv_dir=csv_dir,
        api_key="fake-key",
        run_timestamp="2026-09-22T09:30:00",
        out=out,
    )

    assert exit_code == run_stage1.EXIT_OK
    survivors = _read_table(db_path, "run_results")
    eliminated = _read_table(db_path, "eliminated_results")
    assert set(survivors) | set(eliminated) == {"UFPT", "BROKEN", "USNA", "ATRO"}

    # the 3 good tickers are unaffected - same outcome as the full 23-pilot run
    assert "UFPT" in survivors  # UFPT passes cleanly in the pilot snapshot
    assert "USNA" in survivors
    assert "ATRO" in eliminated  # not_evaluable in the pilot snapshot, for unrelated reasons

    # the broken ticker is isolated, not silently dropped
    assert "BROKEN" in eliminated
    assert eliminated["BROKEN"]["status"] == "not_evaluable"
    assert eliminated["BROKEN"]["invalid_reasons"] == run_stage1.SIMFIN_MAPPING_ERROR

    runs_row = sqlite3.connect(db_path).execute(
        "SELECT companies_evaluated FROM runs WHERE run_timestamp = ?", ("2026-09-22T09:30:00",)
    ).fetchone()
    assert runs_row == (4,)


def test_evaluate_ticker_isolates_a_metric_input_error(monkeypatch):
    # pipeline.evaluate_company() documents MetricInputError as raisable
    # alongside SimFinMappingError - evaluate_ticker() must isolate it the
    # same way (metric_input_error), not let it propagate and abort the
    # whole run (code review of PR #32).
    def _raise(ticker, raw_company, *, thresholds):
        raise MetricInputError("duplicate fiscal_year entries in periods")

    monkeypatch.setattr(run_stage1, "evaluate_company", _raise)
    result = run_stage1.evaluate_ticker("ATRO", {}, thresholds=DEFAULT_THRESHOLDS)
    assert result.status == run_stage1.NOT_EVALUABLE
    assert result.invalid_reasons == (run_stage1.METRIC_INPUT_ERROR,)


def test_run_stage1_a_null_ticker_in_the_statements_batch_does_not_crash_the_run(monkeypatch, tmp_path):
    # A statements-batch record with "ticker": null (key present, value
    # None - real SimFin behavior elsewhere in this codebase, see
    # test_resolve_simfin_id_skips_entries_with_a_null_ticker) used to crash
    # raw_by_ticker's `.get("ticker", "").upper()`. Matching by id instead
    # (code review of PR #32) means it no longer matters at all - the other
    # ticker in the same batch must still be evaluated normally.
    raw_by_ticker = {"UFPT": _load_raw("UFPT"), "USNA": _load_raw("USNA")}
    raw_by_ticker["USNA"] = dict(raw_by_ticker["USNA"], ticker=None)
    _install_fake_simfin(monkeypatch, raw_by_ticker)

    universe_path = tmp_path / "universe.txt"
    universe_path.write_text("UFPT\nUSNA\n", encoding="utf-8")
    db_path = tmp_path / "run_archive.db"
    csv_dir = tmp_path / "runs"

    exit_code = run_stage1.run(
        universe_path=universe_path,
        db_path=db_path,
        csv_dir=csv_dir,
        api_key="fake-key",
        run_timestamp="2026-09-23T09:00:00",
        out=io.StringIO(),
    )

    assert exit_code == run_stage1.EXIT_OK
    survivors = _read_table(db_path, "run_results")
    eliminated = _read_table(db_path, "eliminated_results")
    assert set(survivors) | set(eliminated) == {"UFPT", "USNA"}
    assert "UFPT" in survivors  # unaffected by USNA's null ticker field


def test_run_stage1_batch_record_missing_id_is_eliminated_not_crashed(monkeypatch, tmp_path):
    # A statements-batch record with no usable "id" can't be matched back to
    # the ticker that was requested; it must be eliminated as
    # simfin_statements_missing, not crash the run. Resolution (companies/
    # list) must succeed normally here - only the later statements record is
    # missing its id - to isolate this from the resolve_simfin_id id-guard
    # tested separately in test_simfin_client.py.
    ufpt, usna = _load_raw("UFPT"), _load_raw("USNA")
    companies_list = [{"id": ufpt["id"], "ticker": "UFPT"}, {"id": usna["id"], "ticker": "USNA"}]
    statements = {ufpt["id"]: ufpt, usna["id"]: dict(usna, id=None)}

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        if url == simfin_client.SIMFIN_COMPANIES_LIST_URL:
            return FakeUrlopenResponse(json.dumps(companies_list).encode())
        query = url.split("?", 1)[1]
        params = dict(part.split("=", 1) for part in query.split("&"))
        ids = [int(i) for i in params["id"].split(",")]
        return FakeUrlopenResponse(json.dumps([statements[i] for i in ids if i in statements]).encode())

    monkeypatch.setattr(simfin_client.urllib.request, "urlopen", fake_urlopen)

    universe_path = tmp_path / "universe.txt"
    universe_path.write_text("UFPT\nUSNA\n", encoding="utf-8")
    db_path = tmp_path / "run_archive.db"
    csv_dir = tmp_path / "runs"

    exit_code = run_stage1.run(
        universe_path=universe_path,
        db_path=db_path,
        csv_dir=csv_dir,
        api_key="fake-key",
        run_timestamp="2026-09-23T09:05:00",
        out=io.StringIO(),
    )

    assert exit_code == run_stage1.EXIT_OK
    eliminated = _read_table(db_path, "eliminated_results")
    assert eliminated["USNA"]["invalid_reasons"] == run_stage1.SIMFIN_STATEMENTS_MISSING


def test_run_stage1_a_network_failure_still_isolates_the_batch(monkeypatch, tmp_path):
    # Regression guard for narrowing the bare `except Exception` (code
    # review of PR #32): a genuine network failure must still be isolated
    # to its batch, not propagate.
    def fake_urlopen(req, timeout=None):
        if req.full_url == simfin_client.SIMFIN_COMPANIES_LIST_URL:
            raw = _load_raw("UFPT")
            return FakeUrlopenResponse(json.dumps([{"id": raw["id"], "ticker": raw["ticker"]}]).encode())
        raise simfin_client.urllib.error.URLError("connection refused")

    monkeypatch.setattr(simfin_client.urllib.request, "urlopen", fake_urlopen)

    universe_path = tmp_path / "universe.txt"
    universe_path.write_text("UFPT\n", encoding="utf-8")
    db_path = tmp_path / "run_archive.db"
    csv_dir = tmp_path / "runs"

    exit_code = run_stage1.run(
        universe_path=universe_path,
        db_path=db_path,
        csv_dir=csv_dir,
        api_key="fake-key",
        run_timestamp="2026-09-23T09:10:00",
        out=io.StringIO(),
    )

    assert exit_code == run_stage1.EXIT_OK
    eliminated = _read_table(db_path, "eliminated_results")
    assert eliminated["UFPT"]["invalid_reasons"] == run_stage1.SIMFIN_STATEMENTS_FETCH_FAILED


def test_run_stage1_an_unrelated_bug_in_the_fetch_call_is_not_swallowed(monkeypatch, tmp_path):
    # The narrowed except (urllib.error.URLError, TimeoutError, ValueError)
    # must NOT catch an unrelated programming bug - it should surface, not
    # be silently relabeled as an ordinary data problem (code review of PR
    # #32: a bare `except Exception` previously masked this).
    monkeypatch.setattr(
        run_stage1,
        "fetch_statements_batch",
        lambda ids, api_key: (_ for _ in ()).throw(TypeError("unexpected response shape")),
    )

    def fake_urlopen(req, timeout=None):
        raw = _load_raw("UFPT")
        return FakeUrlopenResponse(json.dumps([{"id": raw["id"], "ticker": raw["ticker"]}]).encode())

    monkeypatch.setattr(simfin_client.urllib.request, "urlopen", fake_urlopen)

    universe_path = tmp_path / "universe.txt"
    universe_path.write_text("UFPT\n", encoding="utf-8")

    with pytest.raises(TypeError, match="unexpected response shape"):
        run_stage1.run(
            universe_path=universe_path,
            db_path=tmp_path / "run_archive.db",
            csv_dir=tmp_path / "runs",
            api_key="fake-key",
            run_timestamp="2026-09-23T09:15:00",
            out=io.StringIO(),
        )


def test_run_stage1_rejects_a_malformed_companies_list_shape(monkeypatch, tmp_path):
    # An HTTP-200 companies/list response that isn't a JSON list (e.g. an
    # error envelope) isn't an exception, so it must be checked explicitly -
    # a clean technical-failure exit, not a mid-loop crash (code review of
    # PR #32).
    def fake_urlopen(req, timeout=None):
        return FakeUrlopenResponse(json.dumps({"error": "invalid api key"}).encode())

    monkeypatch.setattr(simfin_client.urllib.request, "urlopen", fake_urlopen)

    universe_path = tmp_path / "universe.txt"
    universe_path.write_text("UFPT\n", encoding="utf-8")
    db_path = tmp_path / "run_archive.db"

    exit_code = run_stage1.run(
        universe_path=universe_path,
        db_path=db_path,
        csv_dir=tmp_path / "runs",
        api_key="fake-key",
        run_timestamp="2026-09-23T09:20:00",
        out=io.StringIO(),
    )

    assert exit_code == run_stage1.EXIT_TECHNICAL_FAILURE
    assert not db_path.exists()  # aborted before any write was attempted


def test_run_stage1_rolls_back_the_csv_if_the_db_write_then_fails(monkeypatch, tmp_path):
    # save_run_results(, rule_state=None) is the authoritative archive and is written last,
    # after export_survivors_csv() - if it then fails, the already-written
    # CSV is removed again so a nonzero exit code keeps meaning "nothing was
    # written" (Entscheidung 3), not "CSV written, archive silently missing
    # it" (code review of PR #32).
    raw_by_ticker = {"UFPT": _load_raw("UFPT")}
    _install_fake_simfin(monkeypatch, raw_by_ticker)

    def _raise(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(run_stage1, "save_run_results", _raise)

    universe_path = tmp_path / "universe.txt"
    universe_path.write_text("UFPT\n", encoding="utf-8")
    csv_dir = tmp_path / "runs"

    exit_code = run_stage1.run(
        universe_path=universe_path,
        db_path=tmp_path / "run_archive.db",
        csv_dir=csv_dir,
        api_key="fake-key",
        run_timestamp="2026-09-23T09:25:00",
        out=io.StringIO(),
    )

    assert exit_code == run_stage1.EXIT_TECHNICAL_FAILURE
    assert list(csv_dir.glob("*.csv")) == []  # rolled back, not left orphaned


# --- rule state (docs/decisions.md "Regelstand und Kurs je Lauf", Frage 6: the pilot runner writes it too) -----


def test_the_pilot_run_archives_its_rule_state(monkeypatch, tmp_path):
    from store_layer.rule_state import RULE_FILES

    tickers = run_stage1.load_universe(run_stage1.DEFAULT_UNIVERSE_PATH)
    _install_fake_simfin(monkeypatch, {ticker: _load_raw(ticker) for ticker in tickers})
    db_path = tmp_path / "run_archive.db"

    exit_code = run_stage1.run(
        db_path=db_path, csv_dir=tmp_path / "runs", api_key="fake-key", run_timestamp="2026-09-22T09:00:00", out=io.StringIO()
    )

    assert exit_code == run_stage1.EXIT_OK
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT step, run_timestamp FROM run_rule_state").fetchall() == [("stage1", "2026-09-22T09:00:00")]
        assert {r[0] for r in conn.execute("SELECT path FROM run_rule_files")} == set(RULE_FILES)
        assert conn.execute("SELECT prefilter_run_timestamp FROM runs").fetchone() == (None,)  # the pilot has no prefilter run
    finally:
        conn.close()


def test_the_pilot_run_stops_on_a_missing_rule_file_and_writes_nothing(monkeypatch, tmp_path):
    from store_layer.rule_state import RuleStateError

    def broken(**kwargs):
        raise RuleStateError("rule file config/thresholds.yaml not readable")

    monkeypatch.setattr(run_stage1, "capture_rule_state", broken)
    db_path = tmp_path / "run_archive.db"
    exit_code = run_stage1.run(db_path=db_path, csv_dir=tmp_path / "runs", api_key="fake-key", out=io.StringIO())
    assert exit_code == run_stage1.EXIT_TECHNICAL_FAILURE
    assert not db_path.exists() and not (tmp_path / "runs").exists()


def test_the_pilot_run_stores_the_threshold_file_it_really_used(monkeypatch, tmp_path):
    from rule_state_helpers import custom_copy, expected_sha, stored_sha

    tickers = run_stage1.load_universe(run_stage1.DEFAULT_UNIVERSE_PATH)
    _install_fake_simfin(monkeypatch, {ticker: _load_raw(ticker) for ticker in tickers})
    thresholds = custom_copy(tmp_path, run_stage1.DEFAULT_THRESHOLDS_PATH)
    db_path = tmp_path / "run_archive.db"

    exit_code = run_stage1.run(
        db_path=db_path, csv_dir=tmp_path / "runs", api_key="fake-key", thresholds_path=thresholds, out=io.StringIO()
    )

    assert exit_code == run_stage1.EXIT_OK
    assert stored_sha(db_path, "config/thresholds.yaml") == expected_sha(thresholds)

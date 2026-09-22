"""End-to-end tests for run_stage1.py (Issue #9) against the committed
tests/fixtures/simfin_raw/*.json - offline and deterministic, like the rest
of the suite: urllib.request.urlopen is monkeypatched (same pattern as
tests/test_simfin_client.py) so no live SimFin call is ever made.
"""

import io
import json
import sqlite3
from pathlib import Path

from data_layer import simfin_client

import run_stage1

SIMFIN_RAW_DIR = Path(__file__).parent / "fixtures" / "simfin_raw"


def _load_raw(ticker):
    with open(SIMFIN_RAW_DIR / f"{ticker}.json", encoding="utf-8") as f:
        return json.load(f)


class _FakeResponse(io.BytesIO):
    pass


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
            return _FakeResponse(json.dumps(companies_list).encode())
        assert url.startswith(simfin_client.SIMFIN_STATEMENTS_URL)
        query = url.split("?", 1)[1]
        params = dict(part.split("=", 1) for part in query.split("&"))
        ids = [int(i) for i in params["id"].split(",")]
        return _FakeResponse(json.dumps([by_id[i] for i in ids if i in by_id]).encode())

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

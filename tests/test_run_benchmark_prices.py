"""run_benchmark_prices.py: holt die Messlatten-Kurse (Alpha Vantage) und speichert sie in data/benchmark_prices.db."""

import io
import json
import sqlite3
from datetime import date

import pytest

import run_benchmark_prices as run

CFG_TEXT = """
source: alphavantage
pause_between_symbols_seconds: 13
stale_warn_days: 14
gap_calendar_days: 6
lookback_max_calendar_days: 5
jump_threshold_pct: 10
symbols:
  - {symbol: CSPX.LON, role: sp500, currency: USD}
  - {symbol: XDWD.DEX, role: msci_world, currency: EUR}
candidates:
  glob: "docs/kandidaten/runde*.md"
  currency: USD
"""


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def series(*pairs):
    return {"Time Series (Daily)": {d: {"4. close": str(c)} for d, c in pairs}}


def write_list(root, name, tickers):
    rows = "".join(f"| {n} | {n} | {t} | A | passed |\n" for n, t in enumerate(tickers, start=1))
    (root / "docs" / "kandidaten" / name).write_text("| Nr. | Rang | Ticker | Tranche | Status |\n|---|---|---|---|---|\n" + rows, encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "benchmarks.yaml"
    cfg.write_text(CFG_TEXT, encoding="utf-8")
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "SUPERGEHEIM")
    root = tmp_path / "repo"
    (root / "docs" / "kandidaten").mkdir(parents=True)
    write_list(root, "runde1.md", ["AAA"])
    return {"cfg": cfg, "db": tmp_path / "data" / "benchmark_prices.db", "root": root}


def opener_with(answers):
    queue = list(answers)
    urls = []

    def opener(url, timeout=None):
        urls.append(url)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Response(json.dumps(item).encode("utf-8"))

    opener.urls = urls
    return opener


def call(env, answers, sleeps=None, today=date(2026, 10, 9), argv=()):
    sleeps = [] if sleeps is None else sleeps
    answers = list(answers) + [GOOD_C] * max(0, 3 - len(answers))
    code = run.main(
        list(argv),
        opener=opener_with(answers),
        sleep=sleeps.append,
        today=today,
        config_path=env["cfg"],
        db_path=env["db"],
        now=lambda: "2026-10-09T08:00:00",
        key_files=[],
        repo_root=env["root"],
    )
    return code, sleeps


def stored(env):
    connection = sqlite3.connect(env["db"])
    try:
        return connection.execute("SELECT symbol, price_date, close FROM benchmark_prices ORDER BY symbol, price_date").fetchall()
    finally:
        connection.close()


GOOD_A = series(("2026-10-07", 838.10), ("2026-10-08", 838.64))
GOOD_B = series(("2026-10-07", 142.9), ("2026-10-08", 143.31))
GOOD_C = series(("2026-10-07", 20.0), ("2026-10-08", 20.5))


def test_happy_path_stores_both_series_pauses_between_symbols_and_creates_the_data_folder(env, capsys):
    code, sleeps = call(env, [GOOD_A, GOOD_B])
    out = capsys.readouterr().out
    assert code == 0 and sleeps == [13, 13]
    assert len(stored(env)) == 6
    assert "CSPX.LON" in out and "XDWD.DEX" in out and "SUPERGEHEIM" not in out


def test_second_run_with_the_same_data_changes_nothing_and_exits_0(env):
    call(env, [GOOD_A, GOOD_B])
    before = stored(env)
    code, _ = call(env, [GOOD_A, GOOD_B])
    assert code == 0 and stored(env) == before


def test_limit_message_on_the_second_symbol_keeps_the_first_logs_the_failure_and_exits_1(env, capsys):
    code, _ = call(env, [GOOD_A, {"Information": "Burst pattern: 1 request per second."}])
    assert code == 1
    assert {row[0] for row in stored(env)} == {"CSPX.LON"}
    connection = sqlite3.connect(env["db"])
    statuses = connection.execute("SELECT symbol, status FROM benchmark_fetches ORDER BY symbol").fetchall()
    connection.close()
    assert statuses == [("CSPX.LON", "ok"), ("XDWD.DEX", "limit")]
    assert "XDWD.DEX" in capsys.readouterr().out


def test_network_error_is_reported_and_exits_1_without_stopping_the_other_symbol(env):
    import urllib.error

    code, _ = call(env, [urllib_error(), GOOD_B])
    assert code == 1 and {row[0] for row in stored(env)} == {"XDWD.DEX", "AAA"}


def urllib_error():
    import urllib.error

    return urllib.error.URLError("kaputt SUPERGEHEIM")


def test_missing_key_prints_one_line_and_exits_2_without_creating_the_database(env, monkeypatch, capsys):
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY")
    code, _ = call(env, [])
    assert code == 2 and not env["db"].exists()
    assert "ALPHAVANTAGE_API_KEY" in capsys.readouterr().out


def test_conflict_and_gap_make_the_exit_code_1_and_are_printed(env, capsys):
    call(env, [series(("2026-09-01", 100), ("2026-09-02", 101), ("2026-09-03", 102)), GOOD_B], argv=())
    code, _ = call(env, [series(("2026-09-01", 55), ("2026-10-07", 103), ("2026-10-08", 104)), GOOD_B])
    out = capsys.readouterr().out
    assert code == 1 and "Konflikt" in out and "Lücke" in out


def test_stale_series_is_warned_even_if_the_fetch_succeeds(env, capsys):
    code, _ = call(env, [GOOD_A, GOOD_B], today=date(2026, 11, 20))
    assert code == 1 and "Tage alt" in capsys.readouterr().out


def test_key_never_appears_in_output_or_database(env, capsys):
    call(env, [{"Information": "Invalid key SUPERGEHEIM"}, GOOD_B])
    assert "SUPERGEHEIM" not in capsys.readouterr().out
    assert b"SUPERGEHEIM" not in env["db"].read_bytes()


def test_default_database_path_is_inside_the_data_folder_and_the_real_archive_is_never_named():
    assert run.DEFAULT_DB_PATH.name == "benchmark_prices.db" and run.DEFAULT_DB_PATH.parent.name == "data"
    assert "run_archive" not in str(run.DEFAULT_DB_PATH)


def test_a_storage_error_on_one_symbol_is_reported_and_the_other_symbol_still_runs(env, monkeypatch, capsys):
    original = run.benchmark_store.store_fetch

    def failing(connection, config, symbol, rows, fetched_at):
        if symbol == "CSPX.LON":
            raise sqlite3.OperationalError("disk I/O error")
        return original(connection, config, symbol, rows, fetched_at)

    monkeypatch.setattr(run.benchmark_store, "store_fetch", failing)
    code, _ = call(env, [GOOD_A, GOOD_B])
    out = capsys.readouterr().out
    assert code == 1 and "SPEICHERN FEHLGESCHLAGEN" in out
    assert {row[0] for row in stored(env)} == {"XDWD.DEX", "AAA"}
    connection = sqlite3.connect(env["db"])
    status = connection.execute("SELECT status FROM benchmark_fetches WHERE symbol='CSPX.LON'").fetchone()
    connection.close()
    assert status == ("store_error",)


def test_runner_does_not_depend_on_the_one_off_probe_script():
    assert "probe_alphavantage" not in open(run.__file__, encoding="utf-8").read()


def test_candidates_come_from_the_list_files_and_a_new_round_is_picked_up_without_code_changes(env):
    write_list(env["root"], "runde2.md", ["AAA", "BBB"])
    code, _ = call(env, [GOOD_A, GOOD_B, GOOD_C, GOOD_C])
    assert code == 0 and {row[0] for row in stored(env)} == {"CSPX.LON", "XDWD.DEX", "AAA", "BBB"}


def test_a_ticker_dropped_from_the_list_is_no_longer_fetched_but_its_prices_stay(env):
    write_list(env["root"], "runde2.md", ["BBB"])
    call(env, [GOOD_A, GOOD_B, GOOD_C, GOOD_C])
    write_list(env["root"], "runde1.md", ["AAA"])
    (env["root"] / "docs" / "kandidaten" / "runde2.md").unlink()
    opener = opener_with([GOOD_A, GOOD_B, GOOD_C])
    run.main([], opener=opener, sleep=lambda s: None, today=date(2026, 10, 9), config_path=env["cfg"], db_path=env["db"],
             now=lambda: "2026-10-09T09:00:00", key_files=[], repo_root=env["root"])
    assert len(opener.urls) == 3 and not any("BBB" in u for u in opener.urls)
    assert "BBB" in {row[0] for row in stored(env)}


def test_the_daily_limit_stops_the_run_cleanly_and_the_next_run_starts_with_the_oldest_candidate(env, capsys):
    write_list(env["root"], "runde1.md", ["AAA", "BBB", "CCC"])
    limit = {"Information": "Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day."}
    code, sleeps = call(env, [GOOD_A, GOOD_B, GOOD_C, limit])
    out = capsys.readouterr().out
    assert code == 1 and len(sleeps) == 3 and "Tageslimit erreicht: 2 von 5" in out
    assert {row[0] for row in stored(env)} == {"CSPX.LON", "XDWD.DEX", "AAA"}
    opener = opener_with([GOOD_A, GOOD_B, GOOD_C, GOOD_C, GOOD_C])
    run.main([], opener=opener, sleep=lambda s: None, today=date(2026, 10, 9), config_path=env["cfg"], db_path=env["db"],
             now=lambda: "2026-10-10T08:00:00", key_files=[], repo_root=env["root"])
    assert "BBB" in opener.urls[2] and "CCC" in opener.urls[3] and "AAA" in opener.urls[4]


def test_a_failed_candidate_fetch_does_not_stop_the_other_symbols(env):
    code, _ = call(env, [GOOD_A, GOOD_B, {"Error Message": "Invalid API call."}])
    assert code == 1 and {row[0] for row in stored(env)} == {"CSPX.LON", "XDWD.DEX"}

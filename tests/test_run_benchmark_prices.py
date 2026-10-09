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
"""


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def series(*pairs):
    return {"Time Series (Daily)": {d: {"4. close": str(c)} for d, c in pairs}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "benchmarks.yaml"
    cfg.write_text(CFG_TEXT, encoding="utf-8")
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "SUPERGEHEIM")
    monkeypatch.setattr(run, "load_dotenv_if_present", lambda *a, **k: None)
    return {"cfg": cfg, "db": tmp_path / "data" / "benchmark_prices.db"}


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
    code = run.main(
        list(argv),
        opener=opener_with(answers),
        sleep=sleeps.append,
        today=today,
        config_path=env["cfg"],
        db_path=env["db"],
        now=lambda: "2026-10-09T08:00:00",
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


def test_happy_path_stores_both_series_pauses_between_symbols_and_creates_the_data_folder(env, capsys):
    code, sleeps = call(env, [GOOD_A, GOOD_B])
    out = capsys.readouterr().out
    assert code == 0 and sleeps == [13]
    assert len(stored(env)) == 4
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
    assert code == 1 and {row[0] for row in stored(env)} == {"XDWD.DEX"}


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
    assert code == 1 and "älter" in capsys.readouterr().out


def test_key_never_appears_in_output_or_database(env, capsys):
    call(env, [{"Information": "Invalid key SUPERGEHEIM"}, GOOD_B])
    assert "SUPERGEHEIM" not in capsys.readouterr().out
    assert b"SUPERGEHEIM" not in env["db"].read_bytes()


def test_default_database_path_is_inside_the_data_folder_and_the_real_archive_is_never_named():
    assert run.DEFAULT_DB_PATH.name == "benchmark_prices.db" and run.DEFAULT_DB_PATH.parent.name == "data"
    assert "run_archive" not in str(run.DEFAULT_DB_PATH)

"""data/benchmark_prices.db (docs/decisions.md, "Messlatte - Kursreihen speichern und regelmaessig abrufen")."""

import sqlite3
from datetime import date

import pytest

from config_layer.benchmarks import load_benchmarks
from store_layer import benchmark_store as store

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


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "b.yaml"
    path.write_text(CFG_TEXT, encoding="utf-8")
    return load_benchmarks(path)


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "benchmark_prices.db")
    yield connection
    connection.close()


def put(conn, cfg, rows, symbol="CSPX.LON", fetched_at="2026-10-09T08:00:00"):
    return store.store_fetch(conn, cfg, symbol, rows, fetched_at)


def prices(conn, symbol="CSPX.LON"):
    return conn.execute(
        "SELECT price_date, close, provisional, flag, currency FROM benchmark_prices WHERE symbol=? ORDER BY price_date",
        (symbol,),
    ).fetchall()


def test_first_fetch_stores_everything_and_marks_only_the_newest_day_provisional(conn, cfg):
    report = put(conn, cfg, {"2026-10-05": 100.0, "2026-10-06": 101.0, "2026-10-07": 102.0})
    assert report.inserted == 3 and not report.warnings()
    assert prices(conn) == [
        ("2026-10-05", 100.0, 0, None, "USD"),
        ("2026-10-06", 101.0, 0, None, "USD"),
        ("2026-10-07", 102.0, 1, None, "USD"),
    ]
    fetch = conn.execute("SELECT symbol, rows_returned, first_date, last_date, status FROM benchmark_fetches").fetchone()
    assert fetch == ("CSPX.LON", 3, "2026-10-05", "2026-10-07", "ok")


def test_same_fetch_twice_changes_nothing(conn, cfg):
    rows = {"2026-10-05": 100.0, "2026-10-06": 101.0}
    put(conn, cfg, rows)
    before = prices(conn)
    report = put(conn, cfg, rows, fetched_at="2026-10-10T08:00:00")
    assert prices(conn) == before
    assert report.inserted == 0 and report.unchanged == 2 and not report.conflicts


def test_provisional_price_is_replaced_without_conflict_and_the_old_day_becomes_final(conn, cfg):
    put(conn, cfg, {"2026-10-05": 100.0, "2026-10-06": 101.0})
    report = put(conn, cfg, {"2026-10-05": 100.0, "2026-10-06": 101.5, "2026-10-07": 102.0})
    assert report.replaced_provisional == 1 and not report.conflicts
    assert prices(conn) == [
        ("2026-10-05", 100.0, 0, None, "USD"),
        ("2026-10-06", 101.5, 0, None, "USD"),
        ("2026-10-07", 102.0, 1, None, "USD"),
    ]


def test_a_changed_final_price_is_never_overwritten_but_reported(conn, cfg):
    put(conn, cfg, {"2026-10-05": 100.0, "2026-10-06": 101.0, "2026-10-07": 102.0})
    report = put(conn, cfg, {"2026-10-05": 55.0, "2026-10-06": 101.0, "2026-10-07": 102.0})
    assert [row[1] for row in prices(conn)] == [100.0, 101.0, 102.0]
    assert len(report.conflicts) == 1 and report.warnings()
    assert conn.execute(
        "SELECT symbol, price_date, stored_close, new_close FROM benchmark_price_conflicts"
    ).fetchall() == [("CSPX.LON", "2026-10-05", 100.0, 55.0)]


def test_repeating_the_same_conflict_is_not_recorded_twice(conn, cfg):
    put(conn, cfg, {"2026-10-05": 100.0, "2026-10-06": 101.0})
    for day in ("2026-10-10", "2026-10-11"):
        put(conn, cfg, {"2026-10-05": 55.0, "2026-10-06": 101.0}, fetched_at=f"{day}T08:00:00")
    assert conn.execute("SELECT COUNT(*) FROM benchmark_price_conflicts").fetchone()[0] == 1


@pytest.mark.parametrize("bad", [0.0, -3.0, float("nan"), float("inf"), "x", None])
def test_non_positive_or_non_numeric_price_is_rejected_not_stored(conn, cfg, bad):
    report = put(conn, cfg, {"2026-10-05": 100.0, "2026-10-06": bad, "2026-10-07": 101.0})
    assert [row[0] for row in prices(conn)] == ["2026-10-05", "2026-10-07"]
    assert report.rejected == ["2026-10-06"] and report.warnings()


def test_invalid_date_is_rejected(conn, cfg):
    report = put(conn, cfg, {"2026-13-45": 100.0, "2026-10-06": 101.0})
    assert report.rejected == ["2026-13-45"] and len(prices(conn)) == 1


def test_a_jump_above_the_threshold_is_stored_but_flagged_and_reported(conn, cfg):
    report = put(conn, cfg, {"2026-10-05": 100.0, "2026-10-06": 120.0, "2026-10-07": 121.0})
    assert [(row[0], row[3]) for row in prices(conn)] == [
        ("2026-10-05", None), ("2026-10-06", "jump"), ("2026-10-07", None)
    ]
    assert report.jumps == ["2026-10-06"] and report.warnings()


def test_a_jump_exactly_at_the_threshold_is_not_flagged(conn, cfg):
    report = put(conn, cfg, {"2026-10-05": 100.0, "2026-10-06": 110.0})
    assert not report.jumps


def test_gap_inside_one_fetch_is_reported(conn, cfg):
    report = put(conn, cfg, {"2026-10-01": 100.0, "2026-10-12": 101.0})
    assert report.gaps and "2026-10-01" in report.gaps[0] and report.warnings()


def test_gap_of_exactly_the_threshold_is_not_reported_easter_and_new_year_pass(conn, cfg):
    assert not put(conn, cfg, {"2026-04-02": 100.0, "2026-04-08": 101.0}).gaps  # Do bis Mi nach Ostern: 6 Tage


def test_gap_at_the_edge_between_two_fetches_is_reported_but_overlap_is_not(conn, cfg):
    put(conn, cfg, {"2026-09-01": 100.0, "2026-09-02": 101.0})
    overlap = put(conn, cfg, {"2026-09-02": 101.0, "2026-09-03": 102.0})
    assert not overlap.gaps
    gap = put(conn, cfg, {"2026-10-05": 103.0, "2026-10-06": 104.0})
    assert gap.gaps and "2026-09-03" in gap.gaps[0]


def test_each_symbol_is_stored_with_its_configured_currency(conn, cfg):
    put(conn, cfg, {"2026-10-05": 100.0}, symbol="CSPX.LON")
    put(conn, cfg, {"2026-10-05": 143.0}, symbol="XDWD.DEX")
    assert prices(conn, "XDWD.DEX")[0][4] == "EUR" and prices(conn, "CSPX.LON")[0][4] == "USD"


def test_unknown_symbol_is_refused_and_nothing_is_written(conn, cfg):
    with pytest.raises(store.BenchmarkStoreError):
        put(conn, cfg, {"2026-10-05": 100.0}, symbol="ABC.XYZ")
    assert conn.execute("SELECT COUNT(*) FROM benchmark_prices").fetchone()[0] == 0


def test_a_failure_midway_leaves_nothing_of_that_symbol(conn, cfg, monkeypatch):
    original = store._insert_price
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("boom")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_insert_price", flaky)
    with pytest.raises(sqlite3.OperationalError):
        put(conn, cfg, {"2026-10-05": 100.0, "2026-10-06": 101.0, "2026-10-07": 102.0})
    assert prices(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM benchmark_fetches").fetchone()[0] == 0


def test_a_failed_second_symbol_keeps_the_first(conn, cfg):
    put(conn, cfg, {"2026-10-05": 100.0}, symbol="CSPX.LON")
    with pytest.raises(store.BenchmarkStoreError):
        put(conn, cfg, {"2026-10-05": 1.0}, symbol="ABC.XYZ")
    assert len(prices(conn, "CSPX.LON")) == 1


def test_record_failed_fetch_is_logged_without_prices(conn, cfg):
    store.record_failed_fetch(conn, "CSPX.LON", "2026-10-09T08:00:00", "limit", "Tageslimit erreicht")
    assert conn.execute("SELECT status, message FROM benchmark_fetches").fetchone() == ("limit", "Tageslimit erreicht")
    assert prices(conn) == []


def test_read_close_exact_day_weekend_fallback_and_lookback_limit(conn, cfg):
    put(conn, cfg, {"2026-10-02": 100.0, "2026-10-05": 101.0, "2026-10-06": 102.0})
    assert store.read_close(conn, cfg, "CSPX.LON", date(2026, 10, 5)) == ("2026-10-05", 101.0)
    assert store.read_close(conn, cfg, "CSPX.LON", date(2026, 10, 4)) == ("2026-10-02", 100.0)  # Sonntag
    assert store.read_close(conn, cfg, "CSPX.LON", date(2026, 10, 12)) is None  # letzter Kurs 6 Tage davor > 5
    assert store.read_close(conn, cfg, "CSPX.LON", date(2026, 9, 1)) is None  # vor dem ersten Kurs
    assert store.read_close(conn, cfg, "XDWD.DEX", date(2026, 10, 5)) is None  # andere Reihe leer


def test_staleness_check(conn, cfg):
    assert "kein" in store.stale_warning(conn, cfg, "CSPX.LON", date(2026, 10, 9)).lower()
    put(conn, cfg, {"2026-10-01": 100.0})
    assert store.stale_warning(conn, cfg, "CSPX.LON", date(2026, 10, 15)) is None  # 14 Tage
    assert "2026-10-01" in store.stale_warning(conn, cfg, "CSPX.LON", date(2026, 10, 16))  # 15 Tage

"""Holt die Tageskurse der Messlatten-ETFs und der Kandidaten bei Alpha Vantage und speichert sie in data/benchmark_prices.db.

Entscheidung: docs/decisions.md, "Messlatte - Kursreihen speichern und regelmaessig abrufen" (Beschluss von Konstantin am
09.10.2026). Die Kandidaten kommen aus den Kandidatenlisten (config/benchmarks.yaml, Block candidates); eine neue Liste wird ohne
Codeaenderung uebernommen. Schluessel ALPHAVANTAGE_API_KEY: Umgebung, sonst .env.alphavantage, sonst .env (nur diese
eine Zeile wird gelesen, nie der SimFin-Schluessel). Alpha Vantage liefert kostenlos nur die letzten 100 Handelstage je
Abruf und erlaubt nur wenige Abrufe pro Tag: Erreicht ein Abruf das Tageslimit, endet der Lauf sauber; der naechste Lauf
beginnt mit den Symbolen, deren letzter Kurs am aeltesten ist. Empfehlung: taeglich, mindestens alle vier Wochen.

Es wird nur data/benchmark_prices.db beschrieben, nie data/run_archive.db, und es gibt keinen SimFin-Aufruf.

Aufruf:
    python run_benchmark_prices.py

Exit-Code: 0 = alles gespeichert und ohne Auffaelligkeit; 1 = gespeichert, aber mit Warnung (Konflikt, Luecke, Sprung,
abgelehnter Kurs, gescheiterter Abruf, veraltete Reihe); 2 = Abbruch (Konfiguration oder Schluessel fehlt).
"""

import sqlite3
import sys
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from config_layer.benchmarks import CANDIDATE_ROLE, DEFAULT_BENCHMARKS_PATH, BenchmarksConfigError, load_benchmarks  # noqa: E402
from data_layer.alphavantage_prices import KIND_LIMIT, AlphaVantageError, fetch_daily  # noqa: E402
from data_layer.env_file import resolve_api_key  # noqa: E402
from store_layer import benchmark_store  # noqa: E402

DEFAULT_DB_PATH = _REPO_ROOT / "data" / "benchmark_prices.db"


def fetch_order(connection, config):
    """ETFs zuerst, danach die uebrigen Symbole, deren letzter Abruf am laengsten zurueckliegt (nie abgerufen = zuerst).
    Ein am Tageslimit gescheiterter Versuch zaehlt nicht; ein dauerhaft scheiternder Ticker rueckt dagegen nach hinten."""
    def last_day(symbol):
        return connection.execute(
            "SELECT MAX(fetched_at) FROM benchmark_fetches WHERE symbol = ? AND status != 'limit'", (symbol,)
        ).fetchone()[0] or ""

    etfs = [s for s in config.symbols if s.role != CANDIDATE_ROLE]
    candidates = sorted((s for s in config.symbols if s.role == CANDIDATE_ROLE), key=lambda s: (last_day(s.symbol), s.symbol))
    return etfs + candidates


def main(
    argv=None,
    opener=urllib.request.urlopen,
    sleep=time.sleep,
    today=None,
    config_path=DEFAULT_BENCHMARKS_PATH,
    db_path=DEFAULT_DB_PATH,
    now=None,
    key_files=None,
    repo_root=_REPO_ROOT,
):
    today = today or date.today()
    now = now or (lambda: datetime.now().isoformat(timespec="seconds"))
    try:
        config = load_benchmarks(config_path, repo_root)
        api_key = resolve_api_key(key_files)
    except (BenchmarksConfigError, RuntimeError) as exc:
        print(f"ABBRUCH: {exc}")
        return 2

    warnings = []
    connection = benchmark_store.connect(db_path)
    try:
        order = fetch_order(connection, config)
        for number, entry in enumerate(order):
            if number:
                sleep(config.pause_between_symbols_seconds)
            try:
                rows = fetch_daily(entry.symbol, api_key, opener=opener)
            except AlphaVantageError as exc:
                benchmark_store.record_failed_fetch(connection, entry.symbol, now(), exc.kind, str(exc))
                line = f"{entry.symbol}: FEHLER ({exc.kind}): {exc}"
                print(line)
                warnings.append(line)
                if exc.kind == KIND_LIMIT:
                    rest = [s.symbol for s in order[number + 1:]]
                    line = f"Tageslimit erreicht: {len(rest) + 1} von {len(order)} Symbolen nicht abgerufen, naechster Lauf macht weiter"
                    print(line)
                    warnings.append(line)
                    break
                continue
            try:
                report = benchmark_store.store_fetch(connection, config, entry.symbol, rows, now())
            except (sqlite3.Error, benchmark_store.BenchmarkStoreError) as exc:
                line = f"{entry.symbol}: SPEICHERN FEHLGESCHLAGEN ({type(exc).__name__})"
                try:
                    benchmark_store.record_failed_fetch(connection, entry.symbol, now(), "store_error", type(exc).__name__)
                except sqlite3.Error:
                    pass
                print(line)
                warnings.append(line)
                continue
            print(
                f"{entry.symbol}: {report.inserted} neu, {report.replaced_provisional} vorläufig ersetzt, "
                f"{report.unchanged} unverändert"
            )
            for line in report.warnings():
                print(f"  WARNUNG {line}")
                warnings.append(line)
        for entry in config.symbols:
            stale = benchmark_store.stale_warning(connection, config, entry.symbol, today)
            if stale:
                print(f"  WARNUNG {stale}")
                warnings.append(stale)
    finally:
        connection.close()

    return 1 if warnings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Holt die Tageskurse der Messlatten-ETFs bei Alpha Vantage und speichert sie in data/benchmark_prices.db.

Entscheidung: docs/decisions.md, "Messlatte - Kursreihen speichern und regelmaessig abrufen" (Beschluss von Konstantin am
09.10.2026). Laeuft auf Konstantins Rechner mit seinem kostenlosen Schluessel (ALPHAVANTAGE_API_KEY in der Umgebung
oder in .env); der Schluessel liegt nicht auf dem Server. Empfehlung: woechentlich, mindestens alle vier Wochen, denn
Alpha Vantage liefert kostenlos nur die letzten 100 Handelstage je Abruf.

Es wird nur data/benchmark_prices.db beschrieben, nie data/run_archive.db, und es gibt keinen SimFin-Aufruf.

Aufruf:
    python run_benchmark_prices.py

Exit-Code: 0 = alles gespeichert und ohne Auffaelligkeit; 1 = gespeichert, aber mit Warnung (Konflikt, Luecke, Sprung,
abgelehnter Kurs, gescheiterter Abruf, veraltete Reihe); 2 = Abbruch (Konfiguration oder Schluessel fehlt).
"""

import sys
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from config_layer.benchmarks import DEFAULT_BENCHMARKS_PATH, BenchmarksConfigError, load_benchmarks  # noqa: E402
from data_layer.alphavantage_prices import AlphaVantageError, fetch_daily  # noqa: E402
from probe_alphavantage import KEY_NAME, load_dotenv_if_present  # noqa: E402
from store_layer import benchmark_store  # noqa: E402

DEFAULT_DB_PATH = _REPO_ROOT / "data" / "benchmark_prices.db"


def _resolve_key():
    load_dotenv_if_present()
    import os

    key = os.environ.get(KEY_NAME, "").strip()
    if not key:
        raise RuntimeError(f"{KEY_NAME} fehlt (weder Umgebung noch .env).")
    return key


def main(
    argv=None,
    opener=urllib.request.urlopen,
    sleep=time.sleep,
    today=None,
    config_path=DEFAULT_BENCHMARKS_PATH,
    db_path=DEFAULT_DB_PATH,
    now=None,
):
    today = today or date.today()
    now = now or (lambda: datetime.now().isoformat(timespec="seconds"))
    try:
        config = load_benchmarks(config_path)
        api_key = _resolve_key()
    except (BenchmarksConfigError, RuntimeError) as exc:
        print(f"ABBRUCH: {exc}")
        return 2

    warnings = []
    connection = benchmark_store.connect(db_path)
    try:
        for number, entry in enumerate(config.symbols):
            if number:
                sleep(config.pause_between_symbols_seconds)
            try:
                rows = fetch_daily(entry.symbol, api_key, opener=opener)
            except AlphaVantageError as exc:
                benchmark_store.record_failed_fetch(connection, entry.symbol, now(), exc.kind, str(exc))
                line = f"{entry.symbol}: FEHLER ({exc.kind}): {exc}"
                print(line)
                warnings.append(line)
                continue
            report = benchmark_store.store_fetch(connection, config, entry.symbol, rows, now())
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

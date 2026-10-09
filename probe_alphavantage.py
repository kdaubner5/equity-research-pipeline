"""Einmalige Pruefung: liefert Alpha Vantage (kostenloser Schluessel) Tageskurse
fuer die beiden Messlatten-ETFs?

Entscheidung: docs/decisions.md, Eintrag "Messlatte - Auswahl der ETFs (Beschluss)
und Kursquelle (Vorschlag)". Das Skript ruft Alpha Vantage je Symbol genau einmal
auf (kostenlos 25 Abrufe pro Tag), schreibt NICHTS in eine Datenbank und gibt nur
Zahlen aus. Der Schluessel (ALPHAVANTAGE_API_KEY, aus der Umgebung oder .env)
wird nie ausgegeben.

Aufruf (auf dem Rechner mit der .env):
    python probe_alphavantage.py
    python probe_alphavantage.py CSPX.LON XDWD.DEX
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "https://www.alphavantage.co/query"
KEY_NAME = "ALPHAVANTAGE_API_KEY"
DEFAULT_ENV_PATH = Path(__file__).resolve().parent / ".env"
# Alpha-Vantage-Schreibweise: .LON = London, .DEX = Xetra (laut deren Dokumentation).
DEFAULT_SYMBOLS = ("CSPX.LON", "XDWD.DEX")
REQUEST_TIMEOUT_SECONDS = 30


def load_dotenv_if_present(path=DEFAULT_ENV_PATH):
    """KEY=VALUE aus .env in os.environ, ohne bestehende Variablen zu ueberschreiben."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def resolve_api_key():
    load_dotenv_if_present()
    key = os.environ.get(KEY_NAME, "").strip()
    if not key:
        raise RuntimeError(f"{KEY_NAME} fehlt (weder Umgebung noch .env).")
    return key


def fetch_daily(symbol, api_key, opener=urllib.request.urlopen):
    """Ein Abruf TIME_SERIES_DAILY (compact). Gibt das geparste JSON zurueck."""
    query = urllib.parse.urlencode(
        {"function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": "compact", "apikey": api_key}
    )
    with opener(f"{BASE_URL}?{query}", timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def summarize(symbol, payload):
    """Eine Ergebniszeile ohne Schluessel. Meldungen des Anbieters (Limit, Fehler) im Klartext."""
    series = payload.get("Time Series (Daily)")
    if not series:
        # Anbieter-Hinweise (Tageslimit, unbekanntes Symbol, Premium) stehen unter diesen Schluesseln.
        for field in ("Error Message", "Information", "Note"):
            if field in payload:
                return f"{symbol}: KEINE DATEN - {payload[field]}"
        return f"{symbol}: KEINE DATEN - unbekannte Antwort ({sorted(payload)})"
    dates = sorted(series)
    last = dates[-1]
    close = series[last].get("4. close", "?")
    return (
        f"{symbol}: {len(dates)} Handelstage, {dates[0]} bis {last}, "
        f"letzter Schlusskurs {close}"
    )


def main(argv=None, opener=urllib.request.urlopen):
    symbols = tuple(argv if argv else DEFAULT_SYMBOLS)
    api_key = resolve_api_key()
    exit_code = 0
    for symbol in symbols:
        try:
            payload = fetch_daily(symbol, api_key, opener=opener)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            # Die Fehlermeldung kann die URL mit Schluessel enthalten -> nur Typ ausgeben.
            print(f"{symbol}: ABRUF FEHLGESCHLAGEN ({type(exc).__name__})")
            exit_code = 1
            continue
        line = summarize(symbol, payload)
        print(line)
        if "KEINE DATEN" in line:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

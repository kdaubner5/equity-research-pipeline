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

import http.client
import json
import os
import sys
import time
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
# Kostenlos: hoechstens 5 Abrufe pro Minute -> Pause zwischen zwei Symbolen.
PAUSE_BETWEEN_SYMBOLS_SECONDS = 13


def load_dotenv_if_present(path=DEFAULT_ENV_PATH):
    """KEY=VALUE aus .env in os.environ, ohne bestehende Variablen zu ueberschreiben."""
    path = Path(path)
    if not path.exists():
        return
    # utf-8-sig: eine unter Windows gespeicherte .env kann mit einem unsichtbaren BOM beginnen.
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        os.environ.setdefault(key, value.strip().strip("'\""))


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


def _redact(text, api_key):
    text = str(text)
    return text.replace(api_key, "***") if api_key else text


def summarize(symbol, payload, api_key=""):
    """(ok, Ergebniszeile) ohne Schluessel. Meldungen des Anbieters (Limit, Fehler) im Klartext."""
    if not isinstance(payload, dict):
        return False, f"{symbol}: KEINE DATEN - unerwartete Antwort ({type(payload).__name__})"
    series = payload.get("Time Series (Daily)")
    if not isinstance(series, dict) or not series:
        # Anbieter-Hinweise (Tageslimit, unbekanntes Symbol, Premium) stehen unter diesen Schluesseln.
        for field in ("Error Message", "Information", "Note"):
            if field in payload:
                return False, f"{symbol}: KEINE DATEN - {_redact(payload[field], api_key)}"
        return False, f"{symbol}: KEINE DATEN - unbekannte Antwort ({sorted(payload)})"
    dates = sorted(series)
    last = dates[-1]
    try:
        close = float(series[last]["4. close"])
    except (KeyError, TypeError, ValueError):
        return False, f"{symbol}: KEINE DATEN - Schlusskurs am {last} fehlt oder ist keine Zahl"
    if not close > 0:
        return False, f"{symbol}: KEINE DATEN - Schlusskurs am {last} ist nicht positiv ({close})"
    return True, (
        f"{symbol}: {len(dates)} Handelstage, {dates[0]} bis {last}, "
        f"letzter Schlusskurs {close:g}"
    )


def main(argv=None, opener=urllib.request.urlopen, sleep=time.sleep):
    symbols = tuple(argv if argv else DEFAULT_SYMBOLS)
    try:
        api_key = resolve_api_key()
    except RuntimeError as exc:
        print(f"ABBRUCH: {exc}")
        return 2
    exit_code = 0
    for number, symbol in enumerate(symbols):
        if number:
            sleep(PAUSE_BETWEEN_SYMBOLS_SECONDS)
        try:
            payload = fetch_daily(symbol, api_key, opener=opener)
            ok, line = summarize(symbol, payload, api_key)
        except urllib.error.HTTPError as exc:
            # Nur Typ und Statuscode; die Meldung kann die URL mit Schluessel enthalten.
            print(f"{symbol}: ABRUF FEHLGESCHLAGEN (HTTPError {exc.code})")
            exit_code = 1
            continue
        except (OSError, http.client.HTTPException, ValueError) as exc:
            print(f"{symbol}: ABRUF FEHLGESCHLAGEN ({type(exc).__name__})")
            exit_code = 1
            continue
        print(line)
        if not ok:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

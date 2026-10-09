"""Alpha Vantage: Tageskurse (TIME_SERIES_DAILY, compact = letzte 100 Handelstage) fuer die Messlatten-ETFs.

Entscheidung: docs/decisions.md, "Messlatte - Auswahl der ETFs (Beschluss) und Kursquelle (Vorschlag)" und
"Messlatte - Kursreihen speichern und regelmaessig abrufen". Ein Abruf je Symbol; der Schluessel steht nie in einer
Fehlermeldung. Die Schnittstelle liefert keine Waehrung - sie kommt aus config/benchmarks.yaml.
"""

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict

BASE_URL = "https://www.alphavantage.co/query"
REQUEST_TIMEOUT_SECONDS = 30

KIND_LIMIT = "limit"
KIND_ERROR = "error"
KIND_FORMAT = "format"
KIND_NETWORK = "network"

_LIMIT_HINTS = ("per day", "per second", "per minute", "rate limit", "burst", "requests per")


class AlphaVantageError(Exception):
    """Ein Abruf lieferte keine verwertbaren Kurse; `kind` ist limit, error, format oder network."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _redact(text, api_key):
    text = str(text)
    return text.replace(api_key, "***") if api_key else text


def parse_daily(payload, api_key) -> Dict[str, str]:
    """Datum -> Schlusskurs (roh, als Text) aus einer Antwort; AlphaVantageError, wenn keine Kurse darin stehen."""
    if not isinstance(payload, dict):
        raise AlphaVantageError(KIND_FORMAT, f"unerwartete Antwort ({type(payload).__name__})")
    series = payload.get("Time Series (Daily)")
    if isinstance(series, dict) and series:
        result = {}
        for day, values in series.items():
            close = values.get("4. close") if isinstance(values, dict) else None
            result[str(day)] = close
        return result
    for field in ("Error Message", "Information", "Note"):
        if field in payload:
            message = _redact(payload[field], api_key)
            lowered = message.lower()
            kind = KIND_LIMIT if field in ("Information", "Note") and any(h in lowered for h in _LIMIT_HINTS) else KIND_ERROR
            raise AlphaVantageError(kind, message)
    raise AlphaVantageError(KIND_FORMAT, f"keine Kurse in der Antwort ({sorted(payload)})")


def fetch_daily(symbol, api_key, opener=urllib.request.urlopen) -> Dict[str, str]:
    """Ein Abruf TIME_SERIES_DAILY (compact). Netzfehler werden zu AlphaVantageError(network) ohne Schluessel."""
    query = urllib.parse.urlencode(
        {"function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": "compact", "apikey": api_key}
    )
    try:
        with opener(f"{BASE_URL}?{query}", timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AlphaVantageError(KIND_NETWORK, f"HTTP-Fehler {exc.code}") from None
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise AlphaVantageError(KIND_NETWORK, f"Abruf fehlgeschlagen ({type(exc).__name__})") from None
    return parse_daily(payload, api_key)

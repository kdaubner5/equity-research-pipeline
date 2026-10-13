"""config/benchmarks.yaml laden und pruefen (docs/decisions.md, "Messlatte - Kursreihen speichern und
regelmaessig abrufen", Punkt 4).

Gleicher Vertrag wie config/thresholds.yaml: jeder Schluessel ist Pflicht, keine Standardwerte im Code, jedes
Problem bricht das Laden ab und nennt den betroffenen Schluessel.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import yaml

from config_layer.candidates import CandidateListError, collect_candidate_tickers

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_BENCHMARKS_PATH = _REPO_ROOT / "config" / "benchmarks.yaml"

_TOP_LEVEL_KEYS = (
    "source",
    "pause_between_symbols_seconds",
    "stale_warn_days",
    "gap_calendar_days",
    "lookback_max_calendar_days",
    "jump_threshold_pct",
    "symbols",
    "candidates",
)
_CANDIDATE_KEYS = ("glob", "currency")
CANDIDATE_ROLE = "candidate"
_SYMBOL_KEYS = ("symbol", "role", "currency")
_CURRENCIES = ("USD", "EUR")


class BenchmarksConfigError(Exception):
    """config/benchmarks.yaml fehlt, ist fehlerhaft oder besteht die Pruefung nicht; die Meldung nennt den Schluessel."""


@dataclass(frozen=True)
class BenchmarkSymbol:
    symbol: str
    role: str
    currency: str


@dataclass(frozen=True)
class BenchmarksConfig:
    source: str
    pause_between_symbols_seconds: float
    stale_warn_days: int
    gap_calendar_days: int
    lookback_max_calendar_days: int
    jump_threshold_pct: float
    symbols: Tuple[BenchmarkSymbol, ...]

    def symbol(self, name):
        for entry in self.symbols:
            if entry.symbol == name:
                return entry
        return None


def _number(raw, key, *, integer, minimum_exclusive=None, minimum=None):
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise BenchmarksConfigError(f"{key} must be a number, got {raw!r}")
    if integer and not float(raw).is_integer():
        raise BenchmarksConfigError(f"{key} must be a whole number, got {raw!r}")
    if minimum_exclusive is not None and not raw > minimum_exclusive:
        raise BenchmarksConfigError(f"{key} must be greater than {minimum_exclusive}, got {raw!r}")
    if minimum is not None and not raw >= minimum:
        raise BenchmarksConfigError(f"{key} must be at least {minimum}, got {raw!r}")
    return int(raw) if integer else raw


def load_benchmarks(path=DEFAULT_BENCHMARKS_PATH, repo_root=_REPO_ROOT) -> BenchmarksConfig:
    path = Path(path)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BenchmarksConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise BenchmarksConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise BenchmarksConfigError(f"{path} must contain a mapping at the top level")

    unknown = sorted(set(data) - set(_TOP_LEVEL_KEYS))
    if unknown:
        raise BenchmarksConfigError(f"{path} has unrecognized key(s): {unknown}")
    for key in _TOP_LEVEL_KEYS:
        if key not in data:
            raise BenchmarksConfigError(f"{path} is missing required key {key}")

    source = data["source"]
    if not isinstance(source, str) or not source.strip():
        raise BenchmarksConfigError(f"source must be a non-empty string, got {source!r}")

    raw_symbols = data["symbols"]
    if not isinstance(raw_symbols, list) or not raw_symbols:
        raise BenchmarksConfigError("symbols must be a non-empty list")
    symbols = []
    seen = set()
    for index, entry in enumerate(raw_symbols):
        if not isinstance(entry, dict):
            raise BenchmarksConfigError(f"symbols[{index}] must be a mapping")
        extra = sorted(set(entry) - set(_SYMBOL_KEYS))
        if extra:
            raise BenchmarksConfigError(f"symbols[{index}] has unrecognized key(s): {extra}")
        for key in _SYMBOL_KEYS:
            if key not in entry:
                raise BenchmarksConfigError(f"symbols[{index}] is missing required key {key}")
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise BenchmarksConfigError(f"symbols[{index}].{key} must be a non-empty string, got {entry[key]!r}")
        if entry["currency"] not in _CURRENCIES:
            raise BenchmarksConfigError(
                f"symbols[{index}].currency must be one of {list(_CURRENCIES)}, got {entry['currency']!r}"
            )
        if entry["symbol"] in seen:
            raise BenchmarksConfigError(f"symbol {entry['symbol']!r} is listed twice")
        seen.add(entry["symbol"])
        symbols.append(BenchmarkSymbol(entry["symbol"], entry["role"], entry["currency"]))

    block = data["candidates"]
    if not isinstance(block, dict) or set(block) != set(_CANDIDATE_KEYS):
        raise BenchmarksConfigError(f"candidates must be a mapping with exactly the keys {list(_CANDIDATE_KEYS)}")
    for key in _CANDIDATE_KEYS:
        if not isinstance(block[key], str) or not block[key].strip():
            raise BenchmarksConfigError(f"candidates.{key} must be a non-empty string, got {block[key]!r}")
    if block["currency"] not in _CURRENCIES:
        raise BenchmarksConfigError(f"candidates.currency must be one of {list(_CURRENCIES)}, got {block['currency']!r}")
    try:
        tickers = collect_candidate_tickers(repo_root, block["glob"])
    except CandidateListError as exc:
        raise BenchmarksConfigError(f"candidates.glob {block['glob']!r}: {exc}") from exc
    for ticker in tickers:
        if ticker in seen:
            raise BenchmarksConfigError(f"candidate {ticker!r} is already listed under symbols")
        seen.add(ticker)
        symbols.append(BenchmarkSymbol(ticker, CANDIDATE_ROLE, block["currency"]))

    return BenchmarksConfig(
        source=source,
        pause_between_symbols_seconds=_number(
            data["pause_between_symbols_seconds"], "pause_between_symbols_seconds", integer=False, minimum=0
        ),
        stale_warn_days=_number(data["stale_warn_days"], "stale_warn_days", integer=True, minimum_exclusive=0),
        gap_calendar_days=_number(data["gap_calendar_days"], "gap_calendar_days", integer=True, minimum_exclusive=0),
        lookback_max_calendar_days=_number(
            data["lookback_max_calendar_days"], "lookback_max_calendar_days", integer=True, minimum_exclusive=0
        ),
        jump_threshold_pct=_number(data["jump_threshold_pct"], "jump_threshold_pct", integer=False, minimum_exclusive=0),
        symbols=tuple(symbols),
    )

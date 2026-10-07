"""Read-only index of the price cache written by simfin_universe.fetch_prices
(data/cache/universe/<as_of>/prices/*.json), for the Stage-2 share-count check (docs/decisions.md
"Stufe 2 - Datenplausibilitaet: Aktienzahl und Investitionen", Abschnitt 1).

For every company it yields the share count of the last priced row (the same row the pre-filter
used for the market cap) and the last positive dividend per share ("Dividend Paid") in the cached
price window. Nothing is fetched, nothing is written, no second data source: the files are the ones
the universe run already cached. A broken file or company record is counted and named, never
ignored silently, and never stops the other files.
"""

import json
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_KINDS = ("prices_batch", "prices_single")
_REQUIRED_COLUMNS = ("Date", "Common Shares Outstanding", "Last Closing Price")
_DIVIDEND_COLUMN = "Dividend Paid"


class PriceCacheError(Exception):
    """The price-cache directory itself cannot be read (a missing directory is a wrong argument,
    not an empty cache)."""


class _RecordError(ValueError):
    pass


@dataclass(frozen=True)
class PriceCacheEntry:
    simfin_id: int
    price_date: date  # date of the last priced row
    shares: float  # Common Shares Outstanding of that row
    dividend_per_share: Optional[float]  # last positive dividend in the window, None if there is none
    dividend_date: Optional[date]


@dataclass
class PriceCacheIndex:
    entries: Dict[int, PriceCacheEntry] = field(default_factory=dict)
    files_read: int = 0  # files read successfully
    files_invalid: List[Tuple[str, str]] = field(default_factory=list)  # (file name, reason)
    companies_invalid: List[Tuple[object, str]] = field(default_factory=list)  # (id or None, reason)
    duplicate_ids: int = 0  # ids found in more than one file (the later price date wins)
    non_usd: int = 0


def _number(value, what):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _RecordError(f"{what} is not a number: {value!r}")
    try:
        if not math.isfinite(value):
            raise _RecordError(f"{what} is not finite: {value!r}")
    except OverflowError:
        raise _RecordError(f"{what} is too large for a float") from None
    return float(value)


def _parse_record(item):
    """PriceCacheEntry of one company record, or None for a non-USD record. Raises _RecordError."""
    company_id = item.get("id")
    if isinstance(company_id, bool) or not isinstance(company_id, int):
        raise _RecordError(f"record without an integer id: {company_id!r}")
    columns, data = item.get("columns"), item.get("data")
    if not isinstance(columns, list) or not isinstance(data, list):
        raise _RecordError("record lacks columns/data")
    missing = [name for name in _REQUIRED_COLUMNS if name not in columns]
    if missing:
        raise _RecordError(f"record lacks column(s) {missing}")
    if item.get("currency") != "USD":
        return None
    i_date, i_shares, i_close = (columns.index(name) for name in _REQUIRED_COLUMNS)
    i_dividend = columns.index(_DIVIDEND_COLUMN) if _DIVIDEND_COLUMN in columns else None
    priced, dividend, dividend_date = None, None, None
    parsed_rows = []
    for raw in data:
        if not isinstance(raw, list) or len(raw) != len(columns):
            raise _RecordError("row does not match the columns")
        try:
            row_date = date.fromisoformat(raw[i_date])
        except (TypeError, ValueError):
            raise _RecordError(f"unparseable date {raw[i_date]!r}") from None
        close = _number(raw[i_close], "Last Closing Price")
        shares = _number(raw[i_shares], "Common Shares Outstanding")
        paid = _number(raw[i_dividend], "Dividend Paid") if i_dividend is not None else None
        parsed_rows.append((row_date, close, shares, paid))
    for row_date, close, shares, paid in sorted(parsed_rows, key=lambda r: r[0]):
        if close is not None and close > 0 and shares is not None and shares > 0:
            priced = (row_date, shares)
        if paid is not None and paid > 0:
            dividend, dividend_date = paid, row_date
    if priced is None:
        raise _RecordError("no row with a positive price and share count")
    return PriceCacheEntry(company_id, priced[0], priced[1], dividend, dividend_date)


def load_price_cache_index(prices_dir) -> PriceCacheIndex:
    """Index every readable company record under prices_dir (*.json, in file-name order).

    Raises PriceCacheError if the directory does not exist. All other problems are recorded in
    the returned index: files_invalid (unreadable, not JSON, wrong kind or shape),
    companies_invalid (a record violating the contract), non_usd, duplicate_ids.
    """
    directory = Path(prices_dir)
    if not directory.is_dir():
        raise PriceCacheError(f"price cache directory not found: {directory}")
    index = PriceCacheIndex()
    for path in sorted(directory.glob("*.json")):
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            index.files_invalid.append((path.name, f"unreadable: {type(exc).__name__}"))
            continue
        if not isinstance(envelope, dict) or envelope.get("kind") not in _KINDS:
            index.files_invalid.append((path.name, "not a prices envelope"))
            continue
        response = envelope.get("response")
        if not isinstance(response, list):
            index.files_invalid.append((path.name, "response is not a list"))
            continue
        index.files_read += 1
        for item in response:
            if not isinstance(item, dict):
                index.companies_invalid.append((None, "record is not an object"))
                continue
            try:
                entry = _parse_record(item)
            except _RecordError as exc:
                company_id = item.get("id")
                index.companies_invalid.append((company_id if isinstance(company_id, int) and not isinstance(company_id, bool) else None, str(exc)))
                continue
            if entry is None:
                index.non_usd += 1
                continue
            existing = index.entries.get(entry.simfin_id)
            if existing is not None:
                index.duplicate_ids += 1
                if entry.price_date <= existing.price_date:
                    continue  # equal date: the first file in name order stays
            index.entries[entry.simfin_id] = entry
    return index

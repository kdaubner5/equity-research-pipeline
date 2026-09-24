"""SimFin universe loader (Issue #39, docs/decisions.md "Phase 2.0a"): the
company listing, per-company price history and the resumable raw-response
cache the universe pre-filter (gates_layer/universe_prefilter.py) runs on.

Every HTTP request goes through RateLimitedClient: a call budget
(--max-calls; every request counts, including 429 retries and single-id
re-queries), >= MIN_CALL_INTERVAL_SECONDS between requests, the existing
client's fixed-delay 429 retry, and a JSONL call log. The parsing functions
are pure and network-free (tested against mocked responses, never live).

Data-integrity rules (CLAUDE.md, docs/decisions.md "Entscheidung 3a"):
- companies/prices/compact has no bulk mode (HTTP 500 without id/ticker) and
  serves at most 10 ids per call. Records are matched by `id`, never by
  position (the response order differs from the request order).
- A company without prices in the window is simply absent from the response
  (no empty entry). The D-1 live run showed 0 of 114 such ids returning data
  when asked alone, and the re-queries cost 114 of 148 price calls
  (docs/decisions.md, "Nachtrag zu Entscheidung 3"). So after all batches at
  most `max_requeries` of the missing ids - spread evenly over the ids sorted
  ascending - are re-queried once on their own, as a control; every other
  missing id is "no_data" without a re-query. A re-query that DOES return
  price data means the batch response silently dropped a company: it is
  recorded in stats.silent_cap_ids and the runner aborts (exit 1). A re-query
  that no longer fits the call budget is "not_fetched", never silently
  "no_data".
- A batch that fails (network error, HTTP error, response violating the
  contract) marks exactly its own ids "fetch_failed" and is not cached, so a
  rerun retries it; one bad batch never aborts the run.
- A rate limit is not one bad batch: it hits every request. After
  `max_consecutive_429` HTTP 429 in a row without a success in between the
  client raises RateLimitAbort and makes no further request (rate_limit_abort,
  docs/decisions.md "Zweiter Nachtrag zu Entscheidung 3"). The remaining ids
  are "not_fetched" (responses already in the cache are still used), so the
  run ends INCOMPLETE like an exhausted budget and a rerun resumes.
"""

import hashlib
import http.client
import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from data_layer.simfin_client import (
    MAX_RETRY_ATTEMPTS,
    REQUEST_TIMEOUT_SECONDS,
    RETRY_DELAY_SECONDS,
    SIMFIN_BASE_URL,
    SIMFIN_COMPANIES_LIST_URL,
)

SIMFIN_GENERAL_BULK_URL = f"{SIMFIN_BASE_URL}/companies/general/compact"
SIMFIN_PRICES_URL = f"{SIMFIN_BASE_URL}/companies/prices/compact"

# API limit, empirically confirmed on 2026-09-24 (docs/decisions.md, "Phase
# 2.0a" Entscheidung 3a): more ids -> HTTP 400 "Price requests are limited to
# 10 companies"; 10 pilot ids came back complete. Technical constant like
# simfin_client.MAX_BATCH_SIZE, not a business threshold.
MAX_PRICE_BATCH_SIZE = 10
# SimFin Free: 2 requests/second (docs "rate-limits"); a 429 was still seen at
# 1 s spacing during the audit (the 90 s retry handles that).
MIN_CALL_INTERVAL_SECONDS = 1.0
# Calendar days added on top of adv_window_trading_days * 7/5 when sizing the
# price window, so holidays cannot leave the window short of trading days.
_CALENDAR_SLACK_DAYS = 21

US_MARKET = "US"
RATE_LIMIT_HEADER = "X-Rate-Limit-Remaining"

REQUIRED_PRICE_COLUMNS = ("Date", "Last Closing Price", "Trading Volume", "Common Shares Outstanding")
REQUIRED_GENERAL_COLUMNS = ("id", "ticker", "name", "market", "industryName", "sectorName", "sectorCode")

# The failure modes of a single request/response that are isolated per batch
# (same set as run_stage1.evaluate_universe: URLError also covers HTTPError,
# ValueError also covers json.JSONDecodeError and PriceResponseError).
NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, ValueError)

# Per-id price outcomes.
PRICES_OK = "ok"
PRICES_NO_DATA = "no_data"
PRICES_FETCH_FAILED = "fetch_failed"
PRICES_NOT_FETCHED = "not_fetched"


class CallBudgetExhausted(Exception):
    """The --max-calls budget is used up; no further HTTP request is made."""


class RateLimitAbort(Exception):
    """`max_consecutive_429` HTTP 429 in a row without a success in between: a
    rate limit is suspected and the client makes no further request. Not one of
    the NETWORK_ERRORS on purpose - it must not be isolated as one failed batch."""


class UniverseDataError(Exception):
    """The company listing has a shape the loader cannot use - a whole-run
    failure (not one company's data problem)."""


class PriceResponseError(ValueError):
    """A prices response violates the contract (unexpected id, missing column,
    unparseable date, ...). A ValueError so it is isolated per batch together
    with the other NETWORK_ERRORS."""


@dataclass
class LoaderStats:
    """Counters for the run report - nothing here is a threshold."""

    batches_total: int = 0
    batches_cached: int = 0
    batches_network: int = 0
    batches_failed: int = 0
    batches_not_fetched: int = 0
    requeries_cached: int = 0
    requeries_network: int = 0
    requery_with_data: int = 0
    requery_empty: int = 0
    requery_failed: int = 0
    requery_not_fetched: int = 0
    missing_total: int = 0  # ids absent from a successful batch response
    missing_requeried: int = 0  # of those, selected for a single-id re-query
    missing_not_requeried: int = 0  # the rest: no_data without a re-query
    silent_cap_ids: list = field(default_factory=list)  # re-queried ids that DID return prices
    cache_invalid: int = 0
    non_usd: int = 0
    errors: list = field(default_factory=list)  # (context, message) of isolated failures


class RateLimitedClient:
    """GET-JSON client with a call budget, request spacing, 429 retry and a call log.

    `max_calls=None` means unlimited (the caller states it explicitly, no
    default). `max_consecutive_429` is likewise explicit: None = no abort, else
    an integer >= 1 - the RateLimitAbort threshold (see the module docstring).
    `sleep`/`clock` are injectable so tests run without waiting.
    """

    def __init__(self, api_key, *, max_calls, max_consecutive_429, call_log_path, sleep=time.sleep, clock=time.monotonic):
        if max_consecutive_429 is not None and (
            isinstance(max_consecutive_429, bool) or not isinstance(max_consecutive_429, int) or max_consecutive_429 < 1
        ):
            raise ValueError(f"max_consecutive_429 must be None or an integer >= 1, got {max_consecutive_429!r}")
        self._api_key = api_key
        self.max_calls = max_calls
        self.max_consecutive_429 = max_consecutive_429
        self._call_log_path = Path(call_log_path) if call_log_path is not None else None
        self._sleep = sleep
        self._clock = clock
        self._last_start = None
        self.calls = 0
        self.http_429 = 0
        self.consecutive_429 = 0  # 429s since the last successful response
        self.rate_limit_abort = None  # message once RateLimitAbort was raised
        self.rate_limit_values_seen = set()
        self.started_at = clock()
        if self._call_log_path is not None:
            self._call_log_path.parent.mkdir(parents=True, exist_ok=True)

    def has_budget(self):
        return self.max_calls is None or self.calls < self.max_calls

    def _reserve_call(self):
        if self.rate_limit_abort is not None:
            raise RateLimitAbort(self.rate_limit_abort)  # aborted: no further request
        if not self.has_budget():
            raise CallBudgetExhausted(f"call budget of {self.max_calls} exhausted")
        if self._last_start is not None:
            wait = MIN_CALL_INTERVAL_SECONDS - (self._clock() - self._last_start)
            if wait > 0:
                self._sleep(wait)
        self._last_start = self._clock()
        self.calls += 1

    def _log(self, endpoint, n_ids, status, seconds, remaining):
        if remaining is not None:
            self.rate_limit_values_seen.add(str(remaining))
        if self._call_log_path is None:
            return
        record = {
            "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "endpoint": endpoint,
            "n_ids": n_ids,
            "status": status,
            "seconds": round(seconds, 3),
            "rate_limit_remaining": remaining,
        }
        with open(self._call_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def get_json(self, url, *, endpoint, n_ids=0):
        """Return (decoded JSON, X-Rate-Limit-Remaining or None).

        Raises CallBudgetExhausted before a request that would exceed the
        budget, the underlying urllib/JSON error for a failed request (after
        MAX_RETRY_ATTEMPTS for HTTP 429, each retry a counted call), and
        RateLimitAbort at the max_consecutive_429-th HTTP 429 in a row (before
        the retry wait) and for every request after that.
        """
        req = urllib.request.Request(url, headers={"Authorization": self._api_key, "Accept": "application/json"})
        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            self._reserve_call()
            started = self._clock()
            try:
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                    status = getattr(resp, "status", 200)
                    remaining = _header(resp, RATE_LIMIT_HEADER)
                    data = json.load(resp)
            except urllib.error.HTTPError as exc:
                self._log(endpoint, n_ids, exc.code, self._clock() - started, _header(exc, RATE_LIMIT_HEADER))
                if exc.code == 429:
                    self.http_429 += 1
                    self.consecutive_429 += 1
                    if self.max_consecutive_429 is not None and self.consecutive_429 >= self.max_consecutive_429:
                        self.rate_limit_abort = (
                            f"rate_limit_abort: {self.consecutive_429} consecutive HTTP 429 without a success in "
                            "between - rate limit suspected, no further request"
                        )
                        raise RateLimitAbort(self.rate_limit_abort) from exc
                    if attempt < MAX_RETRY_ATTEMPTS:
                        if not self.has_budget():
                            raise CallBudgetExhausted(f"call budget of {self.max_calls} exhausted") from exc
                        self._sleep(RETRY_DELAY_SECONDS)
                        continue
                raise
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                self._log(endpoint, n_ids, f"error:{type(exc).__name__}", self._clock() - started, None)
                raise
            except (OSError, http.client.HTTPException) as exc:
                # A connection reset / truncated read while reading the body is
                # not wrapped by urllib; make it the same isolated failure mode
                # as any other network error (cache writes happen outside this
                # method, so a local OSError there still propagates).
                self._log(endpoint, n_ids, f"error:{type(exc).__name__}", self._clock() - started, None)
                raise urllib.error.URLError(exc) from exc
            self.consecutive_429 = 0  # a success ends the series
            self._log(endpoint, n_ids, status, self._clock() - started, remaining)
            return data, remaining
        raise AssertionError("unreachable: the retry loop either returns or raises")  # pragma: no cover


def _header(resp, name):
    headers = getattr(resp, "headers", None)
    return headers.get(name) if headers is not None else None


# --- cache -----------------------------------------------------------------


def _write_envelope(path, envelope):
    """Atomic write (temp file + rename): a crash never leaves a half-written
    cache file that a resumed run could mistake for a complete response."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_envelope(path, *, kind, expect, stats):
    """The cached envelope, or None if absent, unreadable, or not the response
    that was asked for (kind/ids/window differ) - all of which mean "fetch
    it again". An unusable existing file is counted, not ignored silently."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        stats.cache_invalid += 1
        return None
    try:
        envelope = json.loads(text)
    except ValueError:
        stats.cache_invalid += 1
        return None
    if (
        not isinstance(envelope, dict)
        or envelope.get("kind") != kind
        or "response" not in envelope
        or any(envelope.get(key) != value for key, value in expect.items())
    ):
        stats.cache_invalid += 1
        return None
    return envelope


def _now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- universe listing --------------------------------------------------------


@dataclass(frozen=True)
class UniverseEntry:
    simfin_id: int
    ticker: str | None
    name: str | None
    market: str | None
    sector_code: int | None
    sector_name: str | None
    industry_name: str | None

    @property
    def has_classification(self):
        # All three must be present: a partial classification is not a usable
        # peer-group key (0 such entries in the audit bulk of 2026-09-23).
        return self.sector_code is not None and self.sector_name is not None and self.industry_name is not None


@dataclass(frozen=True)
class ListCrosscheck:
    """companies/list vs general/compact - identical in the audit (0
    deviations); counted here so a future drift is visible, never silent."""

    only_in_list: int
    only_in_general: int
    field_mismatches: int
    examples: tuple


@dataclass(frozen=True)
class UniverseSnapshot:
    entries: tuple  # market == US and with an id, ascending by simfin_id
    total_listed: int
    non_us: int
    without_id: int
    crosscheck: ListCrosscheck

    def classified(self):
        return [entry for entry in self.entries if entry.has_classification]

    def problem_tickers(self):
        """(duplicates, blanks) among the classified entries - a duplicate or
        missing ticker there would make the survivors file ambiguous."""
        seen, duplicates, blanks = {}, set(), []
        for entry in self.classified():
            if not entry.ticker:
                blanks.append(entry.simfin_id)
                continue
            key = entry.ticker.upper()
            if key in seen:
                duplicates.add(entry.ticker)
            seen[key] = entry.simfin_id
        return sorted(duplicates), blanks


def _clean_str(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise UniverseDataError(f"expected a string or null in the company listing, got {value!r}")
    value = value.strip()
    return value or None


def _as_int_or_none(value, what):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise UniverseDataError(f"{what} must be an integer or null in the company listing, got {value!r}")
    return value


def parse_general_bulk(response):
    """general/compact bulk response ({"columns": [...], "data": [[...]]}) ->
    (all listed entries incl. non-US, count of rows without an id). Raises
    UniverseDataError for a shape the loader cannot use."""
    if not isinstance(response, dict) or not isinstance(response.get("columns"), list) or not isinstance(response.get("data"), list):
        raise UniverseDataError("general/compact bulk response is not {'columns': [...], 'data': [...]}")
    columns = response["columns"]
    missing = [name for name in REQUIRED_GENERAL_COLUMNS if name not in columns]
    if missing:
        raise UniverseDataError(f"general/compact bulk response lacks column(s) {missing}")
    idx = {name: columns.index(name) for name in REQUIRED_GENERAL_COLUMNS}
    entries, without_id, seen_ids = [], 0, set()
    for row in response["data"]:
        if not isinstance(row, list) or len(row) != len(columns):
            raise UniverseDataError("general/compact bulk response has a row that does not match its columns")
        simfin_id = _as_int_or_none(row[idx["id"]], "id")
        if simfin_id is None:
            without_id += 1
            continue
        if simfin_id in seen_ids:
            raise UniverseDataError(f"general/compact bulk response lists id {simfin_id} twice")
        seen_ids.add(simfin_id)
        entries.append(
            UniverseEntry(
                simfin_id=simfin_id,
                ticker=_clean_str(row[idx["ticker"]]),
                name=_clean_str(row[idx["name"]]),
                market=_clean_str(row[idx["market"]]),
                sector_code=_as_int_or_none(row[idx["sectorCode"]], "sectorCode"),
                sector_name=_clean_str(row[idx["sectorName"]]),
                industry_name=_clean_str(row[idx["industryName"]]),
            )
        )
    return entries, without_id


def crosscheck_companies_list(entries, companies_list):
    """Compare id set, ticker and classification of companies/list with the
    general/compact entries. Returns a ListCrosscheck (counts + examples)."""
    if not isinstance(companies_list, list):
        raise UniverseDataError(f"companies/list returned {type(companies_list).__name__}, expected a list")
    general = {entry.simfin_id: entry for entry in entries}
    listed = {}
    for item in companies_list:
        if isinstance(item, dict) and isinstance(item.get("id"), int) and not isinstance(item.get("id"), bool):
            listed[item["id"]] = item
    only_in_list = sorted(set(listed) - set(general))
    only_in_general = sorted(set(general) - set(listed))
    mismatches = []
    for simfin_id in sorted(set(listed) & set(general)):
        item, entry = listed[simfin_id], general[simfin_id]
        if (
            _clean_str(item.get("ticker")) != entry.ticker
            or item.get("sectorCode") != entry.sector_code
            or _clean_str(item.get("sectorName")) != entry.sector_name
            or _clean_str(item.get("industryName")) != entry.industry_name
        ):
            mismatches.append(simfin_id)
    examples = tuple((only_in_list + only_in_general + mismatches)[:10])
    return ListCrosscheck(len(only_in_list), len(only_in_general), len(mismatches), examples)


def _cached_bulk(client, path, *, kind, url, endpoint, stats):
    envelope = _read_envelope(path, kind=kind, expect={"url": url}, stats=stats)
    if envelope is not None:
        return envelope["response"]
    response, remaining = client.get_json(url, endpoint=endpoint)
    _write_envelope(
        path,
        {"kind": kind, "url": url, "fetched_at_utc": _now_utc(), "rate_limit_remaining": remaining, "response": response},
    )
    return response


def load_universe(client, run_cache_dir, *, stats):
    """The US universe listing (general/compact bulk, cross-checked against
    companies/list), each response cached under run_cache_dir. A network
    failure or an unusable shape propagates - without the listing nothing can
    run (technical failure, exit code 1 in run_universe.py)."""
    run_cache_dir = Path(run_cache_dir)
    general = _cached_bulk(
        client,
        run_cache_dir / "general_bulk.json",
        kind="general_bulk",
        url=SIMFIN_GENERAL_BULK_URL,
        endpoint="general/compact",
        stats=stats,
    )
    companies_list = _cached_bulk(
        client,
        run_cache_dir / "companies_list.json",
        kind="companies_list",
        url=SIMFIN_COMPANIES_LIST_URL,
        endpoint="companies/list",
        stats=stats,
    )
    all_entries, without_id = parse_general_bulk(general)
    crosscheck = crosscheck_companies_list(all_entries, companies_list)
    us_entries = sorted((e for e in all_entries if e.market == US_MARKET), key=lambda e: e.simfin_id)
    return UniverseSnapshot(
        entries=tuple(us_entries),
        total_listed=len(all_entries) + without_id,
        non_us=len(all_entries) - len(us_entries),
        without_id=without_id,
        crosscheck=crosscheck,
    )


# --- prices ------------------------------------------------------------------


@dataclass(frozen=True)
class PriceRow:
    date: date
    close: float | None
    volume: float | None
    shares: float | None


@dataclass(frozen=True)
class PriceOutcome:
    status: str  # PRICES_OK / PRICES_NO_DATA / PRICES_FETCH_FAILED / PRICES_NOT_FETCHED
    rows: tuple = ()  # ascending by date, only for PRICES_OK
    detail: str | None = None


def price_window(as_of, adv_window_trading_days):
    """(start, end) of the price request: as_of and enough calendar days back
    to hold adv_window_trading_days trading rows (derived from the configured
    window, not a separate setting)."""
    calendar_days = math.ceil(adv_window_trading_days * 7 / 5) + _CALENDAR_SLACK_DAYS
    return as_of - timedelta(days=calendar_days), as_of


def _num(value, what):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PriceResponseError(f"{what} is not a number: {value!r}")
    return float(value)


def _parse_price_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise PriceResponseError(f"unparseable price date {value!r}") from None


def parse_price_response(response, requested_ids, *, start, end):
    """Validate a prices/compact response and return {id: (currency, rows)}.

    Raises PriceResponseError if it violates the contract: not a list, an id
    that was not requested (or twice), a missing required column, a row that
    does not match its columns, an unparseable or duplicate or out-of-window
    date, a non-numeric value. Ids absent from the response are simply not in
    the result (the caller re-queries them).
    """
    if not isinstance(response, list):
        raise PriceResponseError(f"prices response is {type(response).__name__}, expected a list")
    requested = set(requested_ids)
    parsed = {}
    for item in response:
        if not isinstance(item, dict):
            raise PriceResponseError("prices response contains a non-object record")
        company_id = item.get("id")
        if isinstance(company_id, bool) or not isinstance(company_id, int):
            raise PriceResponseError(f"prices record without an integer id: {company_id!r}")
        if company_id not in requested:
            raise PriceResponseError(f"prices response contains id {company_id} that was not requested")
        if company_id in parsed:
            raise PriceResponseError(f"prices response contains id {company_id} twice")
        columns, data = item.get("columns"), item.get("data")
        if not isinstance(columns, list) or not isinstance(data, list):
            raise PriceResponseError(f"prices record {company_id} lacks columns/data")
        missing = [name for name in REQUIRED_PRICE_COLUMNS if name not in columns]
        if missing:
            raise PriceResponseError(f"prices record {company_id} lacks column(s) {missing}")
        i_date, i_close, i_volume, i_shares = (columns.index(name) for name in REQUIRED_PRICE_COLUMNS)
        rows, seen_dates = [], set()
        for raw in data:
            if not isinstance(raw, list) or len(raw) != len(columns):
                raise PriceResponseError(f"prices record {company_id} has a row that does not match its columns")
            row_date = _parse_price_date(raw[i_date])
            if not start <= row_date <= end:
                raise PriceResponseError(f"prices record {company_id} has a row dated {row_date}, outside {start}..{end}")
            if row_date in seen_dates:
                raise PriceResponseError(f"prices record {company_id} has date {row_date} twice")
            seen_dates.add(row_date)
            rows.append(
                PriceRow(
                    date=row_date,
                    close=_num(raw[i_close], "Last Closing Price"),
                    volume=_num(raw[i_volume], "Trading Volume"),
                    shares=_num(raw[i_shares], "Common Shares Outstanding"),
                )
            )
        rows.sort(key=lambda r: r.date)
        parsed[company_id] = (item.get("currency"), tuple(rows))
    return parsed


def _request_prices(client, ids, *, start, end, cache_path, kind, stats, cached_counter, network_counter):
    """One prices call (batch or single id), served from the cache if a valid
    envelope exists, else fetched, validated and cached. Returns {id: (currency,
    rows)}. Raises CallBudgetExhausted or a NETWORK_ERRORS member; a response
    that fails validation is never cached."""
    expect = {"ids": sorted(ids), "start": start.isoformat(), "end": end.isoformat()}
    envelope = _read_envelope(cache_path, kind=kind, expect=expect, stats=stats)
    if envelope is not None:
        try:
            parsed = parse_price_response(envelope["response"], ids, start=start, end=end)
        except PriceResponseError:
            stats.cache_invalid += 1  # a cached file that no longer validates is refetched
        else:
            setattr(stats, cached_counter, getattr(stats, cached_counter) + 1)
            return parsed
    url = f"{SIMFIN_PRICES_URL}?id={','.join(str(i) for i in ids)}&start={start.isoformat()}&end={end.isoformat()}"
    response, remaining = client.get_json(url, endpoint="prices/compact", n_ids=len(ids))
    parsed = parse_price_response(response, ids, start=start, end=end)
    _write_envelope(
        cache_path,
        {**expect, "kind": kind, "fetched_at_utc": _now_utc(), "rate_limit_remaining": remaining, "response": response},
    )
    setattr(stats, network_counter, getattr(stats, network_counter) + 1)
    return parsed


def _outcome(currency, rows, stats):
    if currency != "USD":
        stats.non_usd += 1
        return PriceOutcome(PRICES_NO_DATA, detail=f"non_usd_currency:{currency}")
    if not rows:
        return PriceOutcome(PRICES_NO_DATA, detail="empty_series")
    return PriceOutcome(PRICES_OK, rows=rows)


def _key(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def _requery_single(client, company_id, *, start, end, run_cache_dir, stats):
    cache_path = Path(run_cache_dir) / "prices" / f"single_{company_id}_{_key(start, end)}.json"
    try:
        parsed = _request_prices(
            client,
            [company_id],
            start=start,
            end=end,
            cache_path=cache_path,
            kind="prices_single",
            stats=stats,
            cached_counter="requeries_cached",
            network_counter="requeries_network",
        )
    except CallBudgetExhausted:
        stats.requery_not_fetched += 1
        return PriceOutcome(PRICES_NOT_FETCHED, detail="requery_budget_exhausted")
    except RateLimitAbort:
        stats.requery_not_fetched += 1
        return PriceOutcome(PRICES_NOT_FETCHED, detail="requery_rate_limit_abort")
    except NETWORK_ERRORS as exc:
        stats.requery_failed += 1
        stats.errors.append((f"requery id {company_id}", f"{type(exc).__name__}: {exc}"))
        return PriceOutcome(PRICES_FETCH_FAILED, detail=f"requery_error:{type(exc).__name__}")
    if company_id not in parsed:
        stats.requery_empty += 1
        return PriceOutcome(PRICES_NO_DATA, detail="confirmed_empty_after_requery")
    currency, rows = parsed[company_id]
    outcome = _outcome(currency, rows, stats)
    if outcome.status == PRICES_OK:
        stats.requery_with_data += 1
    else:
        stats.requery_empty += 1
    return outcome


def select_evenly(sorted_ids, n):
    """At most n ids spread evenly over sorted_ids (all of them if n >= len,
    none if n == 0). Deterministic for a given list, so a resumed run picks
    the same ids and finds their single-id responses in the cache."""
    total = len(sorted_ids)
    if n >= total:
        return list(sorted_ids)
    return [sorted_ids[i * total // n] for i in range(n)]


def fetch_prices(client, ids, *, as_of, adv_window_trading_days, max_requeries, run_cache_dir, stats, progress=None):
    """Prices for `ids` (in the given order, batches of MAX_PRICE_BATCH_SIZE)
    -> {id: PriceOutcome}, covering every id exactly once.

    Resumable: a batch or single-id response already in the cache is used
    without a call (also after a RateLimitAbort, which turns every id that
    would need a request into "not_fetched"). One call per batch; then,
    after all batches, at most `max_requeries` of the ids missing from their
    batch response are re-queried alone (see the module docstring), the rest
    are "no_data".
    `progress(done, total)` is called after each batch if given.
    """
    ids = list(ids)
    if len(set(ids)) != len(ids):
        raise ValueError("fetch_prices requires unique ids")
    if isinstance(max_requeries, bool) or not isinstance(max_requeries, int) or max_requeries < 0:
        raise ValueError(f"max_requeries must be an integer >= 0, got {max_requeries!r}")
    start, end = price_window(as_of, adv_window_trading_days)
    run_cache_dir = Path(run_cache_dir)
    batches = [ids[i : i + MAX_PRICE_BATCH_SIZE] for i in range(0, len(ids), MAX_PRICE_BATCH_SIZE)]
    outcomes = {}
    missing = []
    for number, batch in enumerate(batches, start=1):
        stats.batches_total += 1
        cache_path = run_cache_dir / "prices" / f"{_key(start, end, *sorted(batch))}.json"
        try:
            parsed = _request_prices(
                client,
                batch,
                start=start,
                end=end,
                cache_path=cache_path,
                kind="prices_batch",
                stats=stats,
                cached_counter="batches_cached",
                network_counter="batches_network",
            )
        except CallBudgetExhausted:
            stats.batches_not_fetched += 1
            for company_id in batch:
                outcomes[company_id] = PriceOutcome(PRICES_NOT_FETCHED, detail="batch_budget_exhausted")
        except RateLimitAbort:
            # the batch that hit the abort and every later one that is not in the cache
            stats.batches_not_fetched += 1
            for company_id in batch:
                outcomes[company_id] = PriceOutcome(PRICES_NOT_FETCHED, detail="batch_rate_limit_abort")
        except NETWORK_ERRORS as exc:
            stats.batches_failed += 1
            stats.errors.append((f"batch {batch[0]}..{batch[-1]}", f"{type(exc).__name__}: {exc}"))
            for company_id in batch:
                outcomes[company_id] = PriceOutcome(PRICES_FETCH_FAILED, detail=f"batch_error:{type(exc).__name__}")
        else:
            for company_id in batch:
                if company_id in parsed:
                    outcomes[company_id] = _outcome(*parsed[company_id], stats)
                else:
                    missing.append(company_id)
        if progress is not None:
            progress(number, len(batches))

    missing.sort()
    selected = set(select_evenly(missing, max_requeries))
    stats.missing_total += len(missing)
    stats.missing_requeried += len(selected)
    for company_id in missing:
        if company_id not in selected:
            stats.missing_not_requeried += 1
            outcomes[company_id] = PriceOutcome(PRICES_NO_DATA, detail="missing_in_batch_not_requeried")
            continue
        outcome = _requery_single(client, company_id, start=start, end=end, run_cache_dir=run_cache_dir, stats=stats)
        if outcome.status == PRICES_OK:
            stats.silent_cap_ids.append(company_id)
        outcomes[company_id] = outcome
    return outcomes

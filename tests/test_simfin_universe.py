"""Tests for data_layer/simfin_universe.py (Issue #39). Offline and
deterministic: urllib.request.urlopen is monkeypatched with a scripted fake
SimFin and time is a fake clock, so no live call and no real waiting."""

import io
import json
import urllib.error
from datetime import date, timedelta

import pytest

from data_layer import simfin_universe as su
from data_layer.simfin_client import MAX_RETRY_ATTEMPTS, RETRY_DELAY_SECONDS

AS_OF = date(2026, 9, 24)
WINDOW = 60
PRICE_COLUMNS = [
    "Date",
    "Dividend Paid",
    "Common Shares Outstanding",
    "Last Closing Price",
    "Adjusted Closing Price",
    "Highest Price",
    "Lowest Price",
    "Opening Price",
    "Trading Volume",
]


class FakeClock:
    """Time only passes through sleep(), so spacing/retry waits are observable."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeResponse(io.BytesIO):
    def __init__(self, payload, headers=None, status=200):
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.headers = headers if headers is not None else {}
        self.status = status


def price_record(company_id, *, currency="USD", days=3, close=10.0, volume=1000, shares=5_000_000, last=None):
    last = last or AS_OF - timedelta(days=1)
    data = []
    for i in range(days):
        d = last - timedelta(days=days - 1 - i)
        data.append([d.isoformat(), None, shares, close, close, close, close, close, volume])
    return {
        "name": f"CO{company_id}",
        "id": company_id,
        "ticker": f"T{company_id}",
        "currency": currency,
        "isin": None,
        "columns": PRICE_COLUMNS,
        "data": data,
    }


class FakeSimFin:
    """Scripted SimFin: prices for `records` ids only (absent ids stay absent,
    like the real API), optional per-call-number failures."""

    def __init__(self, monkeypatch, records=None, general=None, companies_list=None):
        self.records = dict(records or {})
        self.general = general
        self.companies_list = companies_list
        self.requests = []  # (endpoint, [ids])
        self.fail_calls = {}  # 1-based call number -> exception or HTTP code
        self.hide_in_batches = set()  # ids that only show up when requested alone
        self.shuffle = False
        self.extra_id_in_batch = None
        self.headers = {"X-Rate-Limit-Remaining": "0"}
        monkeypatch.setattr(su.urllib.request, "urlopen", self._urlopen)

    def _urlopen(self, req, timeout=None):
        url = req.full_url
        call_number = len(self.requests) + 1
        if url.startswith(su.SIMFIN_PRICES_URL):
            params = dict(part.split("=", 1) for part in url.split("?", 1)[1].split("&"))
            ids = [int(i) for i in params["id"].split(",")]
            self.requests.append(("prices", ids))
        elif url == su.SIMFIN_GENERAL_BULK_URL:
            self.requests.append(("general", []))
        else:
            assert url == su.SIMFIN_COMPANIES_LIST_URL, url
            self.requests.append(("list", []))
        failure = self.fail_calls.get(call_number)
        if isinstance(failure, int):
            raise urllib.error.HTTPError(url, failure, "scripted", {"X-Rate-Limit-Remaining": "0"}, io.BytesIO(b"{}"))
        if failure is not None:
            raise failure
        if url.startswith(su.SIMFIN_PRICES_URL):
            out = [
                self.records[i]
                for i in ids
                if i in self.records and not (len(ids) > 1 and i in self.hide_in_batches)
            ]
            if self.shuffle:
                out.reverse()
            if self.extra_id_in_batch is not None and len(ids) > 1:
                out.append(price_record(self.extra_id_in_batch))
            return FakeResponse(out, self.headers)
        return FakeResponse(self.general if url == su.SIMFIN_GENERAL_BULK_URL else self.companies_list, self.headers)

    def price_requests(self):
        return [ids for endpoint, ids in self.requests if endpoint == "prices"]


@pytest.fixture
def clock():
    return FakeClock()


def make_client(tmp_path, clock, *, max_calls=None):
    return su.RateLimitedClient(
        "test-key", max_calls=max_calls, call_log_path=tmp_path / "call_log.jsonl", sleep=clock.sleep, clock=clock
    )


# --- price window ------------------------------------------------------------


def test_price_window_covers_the_adv_window_with_calendar_slack():
    start, end = su.price_window(AS_OF, 60)
    assert end == AS_OF
    assert (AS_OF - start).days == 105  # ceil(60 * 7/5) + 21


def test_price_window_scales_with_the_configured_window():
    assert (AS_OF - su.price_window(AS_OF, 20)[0]).days == 28 + 21
    assert (AS_OF - su.price_window(AS_OF, 1)[0]).days == 2 + 21


# --- parse_price_response ----------------------------------------------------


def _window():
    return su.price_window(AS_OF, WINDOW)


def test_parse_price_response_sorts_rows_and_reads_the_required_columns():
    record = price_record(7, days=3, close=12.5, volume=200, shares=1000)
    record["data"].reverse()  # descending in the response
    start, end = _window()
    parsed = su.parse_price_response([record], [7, 8], start=start, end=end)
    currency, rows = parsed[7]
    assert currency == "USD"
    assert [r.date for r in rows] == sorted(r.date for r in rows)
    assert (rows[0].close, rows[0].volume, rows[0].shares) == (12.5, 200.0, 1000.0)
    assert 8 not in parsed  # absent ids are simply not in the result


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda r: [{**r[0], "id": 99}], "not requested"),
        (lambda r: [r[0], r[0]], "twice"),
        (lambda r: [{**r[0], "columns": ["Date", "Last Closing Price"]}], "lacks column"),
        (lambda r: [{**r[0], "data": [["2026-09-01", 1]]}], "does not match its columns"),
        (lambda r: {"id": 7}, "expected a list"),
        (lambda r: ["not an object"], "non-object"),
        (lambda r: [{**r[0], "id": True}], "integer id"),
    ],
    ids=["unrequested_id", "duplicate_id", "missing_column", "short_row", "not_a_list", "non_object", "bool_id"],
)
def test_parse_price_response_rejects_contract_violations(mutate, message):
    start, end = _window()
    with pytest.raises(su.PriceResponseError, match=message):
        su.parse_price_response(mutate([price_record(7)]), [7], start=start, end=end)


def test_parse_price_response_rejects_bad_dates_and_values():
    start, end = _window()
    base = price_record(7, days=2)

    def with_row(row):
        return [{**base, "data": [row]}]

    good = base["data"][0]
    with pytest.raises(su.PriceResponseError, match="unparseable"):
        su.parse_price_response(with_row([*good[:0], "not-a-date", *good[1:]]), [7], start=start, end=end)
    with pytest.raises(su.PriceResponseError, match="outside"):
        su.parse_price_response(with_row([(AS_OF + timedelta(days=1)).isoformat(), *good[1:]]), [7], start=start, end=end)
    with pytest.raises(su.PriceResponseError, match="outside"):
        su.parse_price_response(with_row([(start - timedelta(days=1)).isoformat(), *good[1:]]), [7], start=start, end=end)
    with pytest.raises(su.PriceResponseError, match="not a number"):
        su.parse_price_response(with_row([good[0], None, "many", *good[3:]]), [7], start=start, end=end)
    with pytest.raises(su.PriceResponseError, match="twice"):
        su.parse_price_response([{**base, "data": [good, good]}], [7], start=start, end=end)


def test_parse_price_response_keeps_none_values_for_the_prefilter_to_judge():
    record = price_record(7, days=1)
    record["data"][0][3] = None  # Last Closing Price
    record["data"][0][8] = None  # Trading Volume
    start, end = _window()
    _, rows = su.parse_price_response([record], [7], start=start, end=end)[7]
    assert rows[0].close is None and rows[0].volume is None


# --- universe listing --------------------------------------------------------

GENERAL_COLUMNS = ["companyDescription", "endFy", "id", "industryName", "isin", "market", "name", "numEmployees", "sectorCode", "sectorName", "ticker"]


def general_row(company_id, ticker, *, market="US", sector=(106, "Healthcare", "Drug Manufacturers")):
    code, industry, sector_name = sector if sector else (None, None, None)
    return [None, 12, company_id, industry, None, market, f"Company {company_id}", None, code, sector_name, ticker]


def general_response(rows):
    return {"columns": GENERAL_COLUMNS, "data": rows}


def list_response(rows):
    return [
        {"id": r[2], "industryName": r[3], "isin": None, "name": r[6], "sectorCode": r[8], "sectorName": r[9], "ticker": r[10]}
        for r in rows
    ]


def test_load_universe_keeps_us_entries_sorted_and_counts_the_rest(monkeypatch, tmp_path, clock):
    rows = [
        general_row(30, "CCC"),
        general_row(10, "AAA"),
        general_row(20, "DE1", market="DE"),
        general_row(40, "UNC", sector=None),
        general_row(50, "CN1", market="CN"),
        [None, 12, None, None, None, "US", "no id", None, None, None, "NOID"],
    ]
    FakeSimFin(monkeypatch, general=general_response(rows), companies_list=list_response(rows[:5]))
    stats = su.LoaderStats()
    snapshot = su.load_universe(make_client(tmp_path, clock), tmp_path / "run", stats=stats)
    assert [e.simfin_id for e in snapshot.entries] == [10, 30, 40]
    assert snapshot.total_listed == 6
    assert snapshot.non_us == 2
    assert snapshot.without_id == 1
    assert [e.simfin_id for e in snapshot.classified()] == [10, 30]
    unclassified = snapshot.entries[2]
    assert not unclassified.has_classification and unclassified.sector_code is None


def test_a_partial_classification_is_not_a_classification():
    entry = su.UniverseEntry(1, "AAA", "A", "US", 106, None, "Healthcare")
    assert not entry.has_classification


def test_load_universe_second_call_is_served_from_the_cache(monkeypatch, tmp_path, clock):
    rows = [general_row(10, "AAA")]
    fake = FakeSimFin(monkeypatch, general=general_response(rows), companies_list=list_response(rows))
    stats = su.LoaderStats()
    first = su.load_universe(make_client(tmp_path, clock), tmp_path / "run", stats=stats)
    assert len(fake.requests) == 2
    second = su.load_universe(make_client(tmp_path, clock), tmp_path / "run", stats=stats)
    assert len(fake.requests) == 2  # no new calls
    assert second == first


def test_crosscheck_counts_a_drift_between_the_two_bulk_sources(monkeypatch, tmp_path, clock):
    rows = [general_row(10, "AAA"), general_row(20, "BBB"), general_row(30, "CCC")]
    listed = list_response(rows)
    listed[1]["ticker"] = "BBX"  # ticker mismatch
    listed.append({"id": 99, "ticker": "ONLY", "sectorCode": None, "sectorName": None, "industryName": None})
    del listed[2]  # id 30 only in general
    FakeSimFin(monkeypatch, general=general_response(rows), companies_list=listed)
    snapshot = su.load_universe(make_client(tmp_path, clock), tmp_path / "run", stats=su.LoaderStats())
    check = snapshot.crosscheck
    assert (check.only_in_list, check.only_in_general, check.field_mismatches) == (1, 1, 1)
    assert set(check.examples) == {99, 30, 20}


@pytest.mark.parametrize(
    "response",
    [
        [],
        {"columns": ["id"], "data": []},
        {"columns": GENERAL_COLUMNS, "data": [[1, 2]]},
        {"columns": GENERAL_COLUMNS, "data": [general_row(1, "A"), general_row(1, "B")]},
    ],
    ids=["not_a_dict", "missing_columns", "short_row", "duplicate_id"],
)
def test_parse_general_bulk_rejects_unusable_shapes(response):
    with pytest.raises(su.UniverseDataError):
        su.parse_general_bulk(response)


def test_problem_tickers_reports_duplicates_and_blanks_among_classified_entries():
    entries = tuple(
        su.UniverseEntry(i, ticker, "n", "US", 106, "s", "i")
        for i, ticker in [(1, "AAA"), (2, "aaa"), (3, None), (4, "BBB")]
    ) + (su.UniverseEntry(5, "AAA", "n", "US", None, None, None),)  # unclassified: ignored
    snapshot = su.UniverseSnapshot(entries, 5, 0, 0, su.ListCrosscheck(0, 0, 0, ()))
    duplicates, blanks = snapshot.problem_tickers()
    assert duplicates == ["aaa"] and blanks == [3]


# --- client: budget, spacing, retries, call log ------------------------------


def test_requests_are_spaced_at_least_one_second_apart(monkeypatch, tmp_path, clock):
    FakeSimFin(monkeypatch, records={1: price_record(1)})
    client = make_client(tmp_path, clock)
    for _ in range(3):
        client.get_json(f"{su.SIMFIN_PRICES_URL}?id=1", endpoint="prices/compact", n_ids=1)
    assert clock.sleeps == [su.MIN_CALL_INTERVAL_SECONDS, su.MIN_CALL_INTERVAL_SECONDS]
    assert client.calls == 3


def test_a_429_is_retried_after_the_fixed_delay_and_every_attempt_counts(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1)})
    fake.fail_calls = {1: 429, 2: 429}
    client = make_client(tmp_path, clock)
    data, _ = client.get_json(f"{su.SIMFIN_PRICES_URL}?id=1", endpoint="prices/compact", n_ids=1)
    assert data[0]["id"] == 1
    assert clock.sleeps.count(RETRY_DELAY_SECONDS) == 2
    assert client.calls == 3 and client.http_429 == 2


def test_a_429_that_never_clears_raises_after_the_retry_limit(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch)
    fake.fail_calls = {n: 429 for n in range(1, MAX_RETRY_ATTEMPTS + 2)}
    client = make_client(tmp_path, clock)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        client.get_json(f"{su.SIMFIN_PRICES_URL}?id=1", endpoint="prices/compact", n_ids=1)
    assert excinfo.value.code == 429
    assert client.calls == MAX_RETRY_ATTEMPTS


def test_the_budget_stops_before_a_request_that_would_exceed_it(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1)})
    client = make_client(tmp_path, clock, max_calls=2)
    for _ in range(2):
        client.get_json(f"{su.SIMFIN_PRICES_URL}?id=1", endpoint="prices/compact", n_ids=1)
    with pytest.raises(su.CallBudgetExhausted):
        client.get_json(f"{su.SIMFIN_PRICES_URL}?id=1", endpoint="prices/compact", n_ids=1)
    assert len(fake.requests) == 2 and client.calls == 2


def test_a_429_retry_counts_against_the_budget_and_does_not_wait_when_none_is_left(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1)})
    fake.fail_calls = {1: 429}
    client = make_client(tmp_path, clock, max_calls=1)
    with pytest.raises(su.CallBudgetExhausted):
        client.get_json(f"{su.SIMFIN_PRICES_URL}?id=1", endpoint="prices/compact", n_ids=1)
    assert RETRY_DELAY_SECONDS not in clock.sleeps  # no 90 s wait for a retry that cannot happen


def test_a_non_429_http_error_propagates_without_retry(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch)
    fake.fail_calls = {1: 400}
    client = make_client(tmp_path, clock)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        client.get_json(f"{su.SIMFIN_PRICES_URL}?id=1", endpoint="prices/compact", n_ids=1)
    assert excinfo.value.code == 400 and client.calls == 1


def test_a_connection_reset_is_turned_into_an_isolatable_url_error(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch)
    fake.fail_calls = {1: ConnectionResetError("reset by peer")}
    client = make_client(tmp_path, clock)
    with pytest.raises(urllib.error.URLError):
        client.get_json(f"{su.SIMFIN_PRICES_URL}?id=1", endpoint="prices/compact", n_ids=1)


def test_the_call_log_records_status_ids_and_the_rate_limit_header(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1)})
    fake.fail_calls = {1: 429}
    client = make_client(tmp_path, clock)
    client.get_json(f"{su.SIMFIN_PRICES_URL}?id=1", endpoint="prices/compact", n_ids=1)
    lines = [json.loads(line) for line in (tmp_path / "call_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [line["status"] for line in lines] == [429, 200]
    assert all(line["endpoint"] == "prices/compact" and line["n_ids"] == 1 for line in lines)
    assert lines[1]["rate_limit_remaining"] == "0"
    assert client.rate_limit_values_seen == {"0"}


# --- fetch_prices ------------------------------------------------------------


def run_fetch(_fake, tmp_path, clock, ids, *, max_calls=None, stats=None, cache="run"):
    stats = stats if stats is not None else su.LoaderStats()
    client = make_client(tmp_path, clock, max_calls=max_calls)
    outcomes = su.fetch_prices(
        client, ids, as_of=AS_OF, adv_window_trading_days=WINDOW, run_cache_dir=tmp_path / cache, stats=stats
    )
    return outcomes, client, stats


def test_fetch_prices_batches_by_ten_and_matches_by_id_not_position(monkeypatch, tmp_path, clock):
    ids = list(range(1, 26))
    fake = FakeSimFin(monkeypatch, records={i: price_record(i, close=float(i)) for i in ids})
    fake.shuffle = True  # response order differs from request order, like the real API
    outcomes, client, stats = run_fetch(fake, tmp_path, clock, ids)
    assert [len(batch) for batch in fake.price_requests()] == [10, 10, 5]
    assert client.calls == 3 and stats.batches_total == 3 and stats.batches_network == 3
    assert set(outcomes) == set(ids)
    assert all(outcomes[i].status == su.PRICES_OK for i in ids)
    assert all(outcomes[i].rows[-1].close == float(i) for i in ids)  # each id got ITS own rows


def test_a_missing_id_is_requeried_once_alone_and_confirmed_empty_is_no_data(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1), 2: price_record(2)})
    outcomes, client, stats = run_fetch(fake, tmp_path, clock, [1, 2, 3])  # 3 has no prices at all
    assert fake.price_requests() == [[1, 2, 3], [3]]
    assert outcomes[3].status == su.PRICES_NO_DATA and outcomes[3].detail == "confirmed_empty_after_requery"
    assert (stats.requeries_network, stats.requery_empty, stats.requery_with_data) == (1, 1, 0)
    assert client.calls == 2  # the re-query counted


def test_a_requery_that_returns_data_replaces_the_batch_gap(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1), 2: price_record(2, close=7.0)})
    fake.hide_in_batches = {2}  # only shows up when asked for alone: the "silent cap" case
    outcomes, _, stats = run_fetch(fake, tmp_path, clock, [1, 2])
    assert outcomes[2].status == su.PRICES_OK and outcomes[2].rows[-1].close == 7.0
    assert (stats.requery_with_data, stats.requery_empty) == (1, 0)


def test_a_requery_that_no_longer_fits_the_budget_is_not_fetched_never_no_data(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1)})
    outcomes, client, stats = run_fetch(fake, tmp_path, clock, [1, 2, 3], max_calls=1)
    assert outcomes[1].status == su.PRICES_OK
    assert outcomes[2].status == outcomes[3].status == su.PRICES_NOT_FETCHED
    assert stats.requery_not_fetched == 2 and client.calls == 1


def test_batches_beyond_the_budget_are_not_fetched(monkeypatch, tmp_path, clock):
    ids = list(range(1, 26))
    fake = FakeSimFin(monkeypatch, records={i: price_record(i) for i in ids})
    outcomes, client, stats = run_fetch(fake, tmp_path, clock, ids, max_calls=2)
    assert [outcomes[i].status for i in ids[:20]] == [su.PRICES_OK] * 20
    assert [outcomes[i].status for i in ids[20:]] == [su.PRICES_NOT_FETCHED] * 5
    assert stats.batches_not_fetched == 1 and client.calls == 2


def test_a_second_run_is_served_entirely_from_the_cache(monkeypatch, tmp_path, clock):
    ids = list(range(1, 14))
    fake = FakeSimFin(monkeypatch, records={i: price_record(i) for i in ids if i != 5})  # 5: confirmed no data
    first, _, _ = run_fetch(fake, tmp_path, clock, ids)
    calls_after_first = len(fake.requests)
    stats = su.LoaderStats()
    second, client, _ = run_fetch(fake, tmp_path, clock, ids, stats=stats)
    assert len(fake.requests) == calls_after_first and client.calls == 0
    assert second == first
    assert stats.batches_cached == 2 and stats.requeries_cached == 1 and stats.batches_network == 0


def test_resuming_after_a_failed_batch_refetches_only_that_batch(monkeypatch, tmp_path, clock):
    ids = list(range(1, 31))
    fake = FakeSimFin(monkeypatch, records={i: price_record(i) for i in ids})
    fake.fail_calls = {2: 500}  # the second batch fails
    outcomes, _, stats = run_fetch(fake, tmp_path, clock, ids)
    assert [outcomes[i].status for i in range(11, 21)] == [su.PRICES_FETCH_FAILED] * 10
    assert all(outcomes[i].status == su.PRICES_OK for i in list(range(1, 11)) + list(range(21, 31)))
    assert stats.batches_failed == 1 and stats.errors and "batch 11..20" in stats.errors[0][0]
    fake.fail_calls = {}
    before = len(fake.requests)
    outcomes2, client, _ = run_fetch(fake, tmp_path, clock, ids)
    assert len(fake.requests) - before == 1 and client.calls == 1  # only the failed batch
    assert all(outcomes2[i].status == su.PRICES_OK for i in ids)


def test_a_response_with_an_unrequested_id_fails_its_batch_and_is_not_cached(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1), 2: price_record(2)})
    fake.extra_id_in_batch = 999
    outcomes, _, stats = run_fetch(fake, tmp_path, clock, [1, 2])
    assert outcomes[1].status == outcomes[2].status == su.PRICES_FETCH_FAILED
    assert stats.batches_failed == 1
    assert list((tmp_path / "run").glob("prices/*.json")) == []  # the invalid response was never cached
    fake.extra_id_in_batch = None
    outcomes2, client, _ = run_fetch(fake, tmp_path, clock, [1, 2])
    assert client.calls == 1 and all(o.status == su.PRICES_OK for o in outcomes2.values())


def test_a_non_usd_series_is_no_data_with_the_currency_noted(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1, currency="EUR")})
    outcomes, _, stats = run_fetch(fake, tmp_path, clock, [1])
    assert outcomes[1].status == su.PRICES_NO_DATA and outcomes[1].detail == "non_usd_currency:EUR"
    assert stats.non_usd == 1


def test_an_empty_series_is_no_data(monkeypatch, tmp_path, clock):
    record = price_record(1)
    record["data"] = []
    fake = FakeSimFin(monkeypatch, records={1: record})
    outcomes, _, _ = run_fetch(fake, tmp_path, clock, [1])
    assert outcomes[1].status == su.PRICES_NO_DATA and outcomes[1].detail == "empty_series"


def test_a_corrupt_cache_file_is_counted_and_refetched(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1)})
    run_fetch(fake, tmp_path, clock, [1])
    (cache_file,) = (tmp_path / "run" / "prices").glob("*.json")
    cache_file.write_text("{ this is not json", encoding="utf-8")
    stats = su.LoaderStats()
    outcomes, client, _ = run_fetch(fake, tmp_path, clock, [1], stats=stats)
    assert outcomes[1].status == su.PRICES_OK and client.calls == 1
    assert stats.cache_invalid == 1
    json.loads(cache_file.read_text(encoding="utf-8"))  # rewritten, valid again


def test_a_cache_file_for_other_ids_is_not_reused(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1), 2: price_record(2)})
    run_fetch(fake, tmp_path, clock, [1])
    (cache_file,) = (tmp_path / "run" / "prices").glob("*.json")
    envelope = json.loads(cache_file.read_text(encoding="utf-8"))
    envelope["ids"] = [2]  # the file claims to hold a different request
    cache_file.write_text(json.dumps(envelope), encoding="utf-8")
    stats = su.LoaderStats()
    _, client, _ = run_fetch(fake, tmp_path, clock, [1], stats=stats)
    assert client.calls == 1 and stats.cache_invalid == 1


def test_a_different_window_does_not_reuse_the_cache(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1)})
    run_fetch(fake, tmp_path, clock, [1])
    client = make_client(tmp_path, clock)
    su.fetch_prices(
        client, [1], as_of=AS_OF, adv_window_trading_days=90, run_cache_dir=tmp_path / "run", stats=su.LoaderStats()
    )
    assert client.calls == 1  # 90-day window is a different request


def test_fetch_prices_covers_every_id_exactly_once_and_rejects_duplicates(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1)})
    outcomes, _, _ = run_fetch(fake, tmp_path, clock, [3, 1, 2])
    assert set(outcomes) == {1, 2, 3}
    with pytest.raises(ValueError, match="unique"):
        run_fetch(fake, tmp_path, clock, [1, 1])


def test_the_response_headers_are_stored_with_the_raw_response(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1)})
    run_fetch(fake, tmp_path, clock, [1])
    (cache_file,) = (tmp_path / "run" / "prices").glob("*.json")
    envelope = json.loads(cache_file.read_text(encoding="utf-8"))
    assert envelope["kind"] == "prices_batch" and envelope["rate_limit_remaining"] == "0"
    assert envelope["response"][0]["id"] == 1  # the untouched raw response
    assert envelope["ids"] == [1] and envelope["end"] == AS_OF.isoformat()

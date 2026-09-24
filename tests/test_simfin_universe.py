"""Tests for data_layer/simfin_universe.py (Issue #39). Offline and
deterministic: urllib.request.urlopen is monkeypatched with a scripted fake
SimFin and time is a fake clock, so no live call and no real waiting."""

import json
import urllib.error
from datetime import timedelta

import pytest

from data_layer import simfin_universe as su
from data_layer.simfin_client import MAX_RETRY_ATTEMPTS, RETRY_DELAY_SECONDS

from universe_fakes import (  # noqa: F401  (clock is a fixture)
    AS_OF,
    GENERAL_COLUMNS,
    WINDOW,
    FakeSimFin,
    clock,
    general_response,
    general_row,
    list_response,
    price_record,
)


def make_client(tmp_path, clock, *, max_calls=None, max_consecutive_429=None):
    return su.RateLimitedClient(
        "test-key",
        max_calls=max_calls,
        max_consecutive_429=max_consecutive_429,
        call_log_path=tmp_path / "call_log.jsonl",
        sleep=clock.sleep,
        clock=clock,
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


# --- client: rate-limit abort (max_consecutive_429) --------------------------

PRICES_URL_1 = f"{su.SIMFIN_PRICES_URL}?id=1"


def get_one(client):
    return client.get_json(PRICES_URL_1, endpoint="prices/compact", n_ids=1)


def test_the_client_aborts_at_the_nth_consecutive_429_without_waiting_or_asking_again(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1)})
    fake.fail_calls = {n: 429 for n in range(1, 50)}
    client = make_client(tmp_path, clock, max_consecutive_429=5)
    with pytest.raises(su.RateLimitAbort) as excinfo:
        get_one(client)
    assert len(fake.requests) == 5 and client.calls == 5 and client.http_429 == 5
    assert clock.sleeps == [RETRY_DELAY_SECONDS] * 4  # no 90 s wait after the 5th 429
    assert "rate_limit_abort" in str(excinfo.value) and "5 consecutive" in str(excinfo.value)
    assert client.rate_limit_abort == str(excinfo.value)
    with pytest.raises(su.RateLimitAbort):  # after the abort no request goes out at all
        get_one(client)
    assert len(fake.requests) == 5 and client.calls == 5 and clock.sleeps == [RETRY_DELAY_SECONDS] * 4


def test_the_aborting_429_is_still_written_to_the_call_log(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch)
    fake.fail_calls = {n: 429 for n in range(1, 50)}
    client = make_client(tmp_path, clock, max_consecutive_429=3)
    with pytest.raises(su.RateLimitAbort):
        get_one(client)
    lines = [json.loads(line) for line in (tmp_path / "call_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [line["status"] for line in lines] == [429, 429, 429]


def test_one_429_fewer_than_the_limit_followed_by_a_success_does_not_abort(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1)})
    fake.fail_calls = {1: 429, 2: 429, 3: 429, 4: 429}
    client = make_client(tmp_path, clock, max_consecutive_429=5)
    data, _ = get_one(client)  # the 5th attempt succeeds
    assert data[0]["id"] == 1 and client.rate_limit_abort is None
    assert client.consecutive_429 == 0 and client.http_429 == 4


def test_a_success_resets_the_consecutive_count(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1)})
    fake.fail_calls = {**{n: 429 for n in range(1, 5)}, **{n: 429 for n in range(6, 10)}}  # 4 x 429, ok, 4 x 429, ok
    client = make_client(tmp_path, clock, max_consecutive_429=5)
    get_one(client)
    get_one(client)  # 8 429s in total, never 5 in a row
    assert client.http_429 == 8 and client.rate_limit_abort is None and client.consecutive_429 == 0


def test_another_error_between_the_429s_does_not_reset_the_count(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch)
    fake.fail_calls = {1: 429, 2: 429, 3: 500, 4: 429, 5: 429, 6: 429, 7: 429}
    client = make_client(tmp_path, clock, max_consecutive_429=5)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get_one(client)  # 429, 429, then a 500 that is not retried
    assert excinfo.value.code == 500 and client.consecutive_429 == 2
    with pytest.raises(su.RateLimitAbort):
        get_one(client)  # 3 more 429 -> 5 in a row with only a 500 in between
    assert len(fake.requests) == 6


def test_without_a_limit_the_client_never_aborts_on_429(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1)})
    fake.fail_calls = {n: 429 for n in range(1, MAX_RETRY_ATTEMPTS)}  # every attempt but the last
    client = make_client(tmp_path, clock, max_consecutive_429=None)
    data, _ = get_one(client)
    assert data[0]["id"] == 1 and client.http_429 == MAX_RETRY_ATTEMPTS - 1 and client.rate_limit_abort is None


@pytest.mark.parametrize("bad", [0, -1, 2.5, True, "5"])
def test_max_consecutive_429_must_be_none_or_an_integer_of_at_least_one(tmp_path, clock, bad):
    with pytest.raises(ValueError, match="max_consecutive_429"):
        make_client(tmp_path, clock, max_consecutive_429=bad)


# --- fetch_prices ------------------------------------------------------------


def run_fetch(_fake, tmp_path, clock, ids, *, max_calls=None, stats=None, cache="run", max_requeries=20, max_consecutive_429=None):
    stats = stats if stats is not None else su.LoaderStats()
    client = make_client(tmp_path, clock, max_calls=max_calls, max_consecutive_429=max_consecutive_429)
    outcomes = su.fetch_prices(
        client,
        ids,
        as_of=AS_OF,
        adv_window_trading_days=WINDOW,
        max_requeries=max_requeries,
        run_cache_dir=tmp_path / cache,
        stats=stats,
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


def test_a_requery_that_returns_prices_is_recorded_as_a_silent_cap(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1), 2: price_record(2, close=7.0)})
    fake.hide_in_batches = {2}  # only shows up when asked for alone: the "silent cap" case
    outcomes, _, stats = run_fetch(fake, tmp_path, clock, [1, 2])
    assert outcomes[2].status == su.PRICES_OK and outcomes[2].rows[-1].close == 7.0  # the data is kept ...
    assert stats.silent_cap_ids == [2]  # ... and flagged: the runner aborts on this
    assert (stats.requery_with_data, stats.requery_empty) == (1, 0)


def test_a_confirmed_empty_requery_is_not_a_silent_cap(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1)})
    _, _, stats = run_fetch(fake, tmp_path, clock, [1, 2])
    assert stats.silent_cap_ids == []


# --- capped, evenly spread re-queries ---------------------------------------------


@pytest.mark.parametrize(
    "ids,n,expected",
    [
        (list(range(100)), 5, [0, 20, 40, 60, 80]),
        (list(range(10)), 3, [0, 3, 6]),
        ([1, 2, 3], 5, [1, 2, 3]),
        ([1, 2, 3], 3, [1, 2, 3]),
        ([1, 2, 3], 0, []),
        ([], 4, []),
    ],
)
def test_select_evenly_spreads_over_the_sorted_ids(ids, n, expected):
    assert su.select_evenly(ids, n) == expected


def test_select_evenly_never_repeats_an_id_and_stays_within_the_list():
    for total in range(1, 60):
        for n in range(1, total + 1):
            picked = su.select_evenly(list(range(total)), n)
            assert len(picked) == n == len(set(picked)) and picked == sorted(picked) and picked[0] == 0


def test_only_max_requeries_of_the_missing_ids_are_asked_for_and_they_are_spread_evenly(monkeypatch, tmp_path, clock):
    ids = list(range(1, 41))  # 1..10 have prices, 11..40 (30 ids) are missing from their batch responses
    fake = FakeSimFin(monkeypatch, records={i: price_record(i) for i in range(1, 11)})
    outcomes, client, stats = run_fetch(fake, tmp_path, clock, ids, max_requeries=5)
    singles = [batch[0] for batch in fake.price_requests() if len(batch) == 1]
    assert singles == [11, 17, 23, 29, 35]  # indices 0, 6, 12, 18, 24 of the 30 sorted missing ids
    assert client.calls == 4 + 5  # 4 batches + 5 single-id re-queries, not 30
    assert (stats.missing_total, stats.missing_requeried, stats.missing_not_requeried) == (30, 5, 25)
    assert all(outcomes[i].status == su.PRICES_NO_DATA for i in range(11, 41))
    assert {outcomes[i].detail for i in singles} == {"confirmed_empty_after_requery"}
    assert {outcomes[i].detail for i in range(11, 41) if i not in singles} == {"missing_in_batch_not_requeried"}


def test_zero_max_requeries_asks_no_single_id_at_all(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1)})
    outcomes, client, stats = run_fetch(fake, tmp_path, clock, [1, 2, 3], max_requeries=0)
    assert fake.price_requests() == [[1, 2, 3]] and client.calls == 1
    assert outcomes[2].status == outcomes[3].status == su.PRICES_NO_DATA
    assert stats.missing_total == 2 and stats.missing_requeried == 0 and stats.missing_not_requeried == 2


def test_the_requery_selection_is_repeatable_so_a_resumed_run_finds_its_singles_in_the_cache(monkeypatch, tmp_path, clock):
    ids = list(range(1, 41))
    fake = FakeSimFin(monkeypatch, records={i: price_record(i) for i in range(1, 11)})
    run_fetch(fake, tmp_path, clock, ids, max_requeries=5)
    calls_before = len(fake.requests)
    _, client, stats = run_fetch(fake, tmp_path, clock, ids, max_requeries=5)
    assert len(fake.requests) == calls_before and client.calls == 0 and stats.requeries_cached == 5


def test_requeries_that_do_not_fit_the_budget_are_not_fetched_while_unselected_missing_ids_stay_no_data(monkeypatch, tmp_path, clock):
    ids = list(range(1, 21))  # 1..10 have prices, 11..20 are missing from their batch responses
    fake = FakeSimFin(monkeypatch, records={i: price_record(i) for i in range(1, 11)})
    # 2 batches + 1 single-id call fit into the budget of 3; 4 of the 10 missing ids are selected: 11, 13, 16, 18
    outcomes, client, stats = run_fetch(fake, tmp_path, clock, ids, max_requeries=4, max_calls=3)
    assert client.calls == 3
    assert outcomes[11].status == su.PRICES_NO_DATA and outcomes[11].detail == "confirmed_empty_after_requery"
    assert [outcomes[i].status for i in (13, 16, 18)] == [su.PRICES_NOT_FETCHED] * 3  # selected, but no budget left
    assert stats.requery_not_fetched == 3
    for i in (12, 14, 15, 17, 19, 20):  # never selected: no_data without a re-query, not "not_fetched"
        assert outcomes[i].status == su.PRICES_NO_DATA and outcomes[i].detail == "missing_in_batch_not_requeried"


@pytest.mark.parametrize("bad", [-1, 2.5, True, None, "20"])
def test_max_requeries_must_be_a_non_negative_integer(monkeypatch, tmp_path, clock, bad):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1)})
    with pytest.raises(ValueError, match="max_requeries"):
        run_fetch(fake, tmp_path, clock, [1], max_requeries=bad)


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
        client,
        [1],
        as_of=AS_OF,
        adv_window_trading_days=90,
        max_requeries=20,
        run_cache_dir=tmp_path / "run",
        stats=su.LoaderStats(),
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


# --- fetch_prices: rate-limit abort ---------------------------------------------


def limit_from(fake, first_call):
    """Every request from `first_call` on (1-based) answers HTTP 429."""
    fake.fail_calls = {n: 429 for n in range(first_call, first_call + 200)}


def test_a_rate_limit_abort_turns_the_rest_into_not_fetched_and_stops_asking(monkeypatch, tmp_path, clock):
    ids = list(range(1, 36))  # batches of 10, 10, 10, 5
    fake = FakeSimFin(monkeypatch, records={i: price_record(i) for i in ids})
    limit_from(fake, 3)  # the first two batches work, then only 429
    outcomes, client, stats = run_fetch(fake, tmp_path, clock, ids, max_consecutive_429=5)
    assert all(outcomes[i].status == su.PRICES_OK for i in range(1, 21))
    assert all(outcomes[i].status == su.PRICES_NOT_FETCHED and outcomes[i].detail == "batch_rate_limit_abort" for i in range(21, 36))
    assert len(fake.requests) == 2 + 5 and client.calls == 7  # the 4th batch is not even attempted
    assert (stats.batches_network, stats.batches_not_fetched, stats.batches_failed) == (2, 2, 0)
    assert client.rate_limit_abort is not None and stats.missing_total == 0


def test_after_a_rate_limit_abort_batches_already_in_the_cache_are_still_used(monkeypatch, tmp_path, clock):
    ids = list(range(1, 36))
    fake = FakeSimFin(monkeypatch, records={i: price_record(i) for i in ids})
    run_fetch(fake, tmp_path, clock, list(range(21, 31)))  # caches exactly the third batch of the full list
    limit_from(fake, len(fake.requests) + 1)
    outcomes, client, stats = run_fetch(fake, tmp_path, clock, ids, max_consecutive_429=5)
    assert all(outcomes[i].status == su.PRICES_NOT_FETCHED for i in list(range(1, 21)) + list(range(31, 36)))
    assert all(outcomes[i].status == su.PRICES_OK for i in range(21, 31))  # served from the cache, no request
    assert (stats.batches_cached, stats.batches_network, stats.batches_not_fetched) == (1, 0, 3)
    assert client.calls == 5


def test_the_consecutive_429_count_runs_across_a_batch_that_used_up_its_retries(monkeypatch, tmp_path, clock):
    limit = MAX_RETRY_ATTEMPTS + 3  # more than one batch's retries
    ids = list(range(1, 26))
    fake = FakeSimFin(monkeypatch, records={i: price_record(i) for i in ids})
    limit_from(fake, 1)
    outcomes, client, stats = run_fetch(fake, tmp_path, clock, ids, max_consecutive_429=limit)
    assert all(outcomes[i].status == su.PRICES_FETCH_FAILED for i in range(1, 11))  # batch 1 ran out of retries
    assert all(outcomes[i].status == su.PRICES_NOT_FETCHED for i in range(11, 26))  # the abort hit batch 2
    assert (stats.batches_failed, stats.batches_not_fetched) == (1, 2)
    assert len(fake.requests) == limit and client.calls == limit
    assert f"{limit} consecutive" in client.rate_limit_abort


def test_a_rate_limit_abort_in_the_requery_phase_leaves_the_requery_not_fetched(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, records={1: price_record(1), 2: price_record(2)})  # id 3 has no prices
    limit_from(fake, 2)  # the batch works, the re-query of id 3 meets the limit
    outcomes, client, stats = run_fetch(fake, tmp_path, clock, [1, 2, 3], max_consecutive_429=5)
    assert outcomes[1].status == outcomes[2].status == su.PRICES_OK
    assert outcomes[3].status == su.PRICES_NOT_FETCHED and outcomes[3].detail == "requery_rate_limit_abort"  # never no_data
    assert (stats.requery_not_fetched, stats.requery_failed, stats.requery_empty) == (1, 0, 0)
    assert client.calls == 1 + 5


def test_one_success_between_the_429_series_keeps_the_batches_going(monkeypatch, tmp_path, clock):
    ids = list(range(1, 26))
    fake = FakeSimFin(monkeypatch, records={i: price_record(i) for i in ids})
    fake.fail_calls = {1: 429, 2: 429, 3: 429, 4: 429, 6: 429, 7: 429, 8: 429, 9: 429}  # 4 x 429 before batch 1 and batch 2
    outcomes, client, stats = run_fetch(fake, tmp_path, clock, ids, max_consecutive_429=5)
    assert all(o.status == su.PRICES_OK for o in outcomes.values())
    assert client.http_429 == 8 and client.rate_limit_abort is None and stats.batches_network == 3

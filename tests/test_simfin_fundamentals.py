"""Tests for data_layer/simfin_fundamentals.py (Issue #43). Offline and
deterministic: urllib.request.urlopen is monkeypatched with a scripted fake
SimFin and time is a fake clock, so no live call and no real waiting."""

import json
import urllib.error

import pytest

from data_layer import simfin_fundamentals as sf
from data_layer import simfin_universe as su

from universe_fakes import (  # noqa: F401  (clock is a fixture)
    FakeSimFin,
    clock,
    statements_record,
)


def make_client(tmp_path, clock, *, max_calls=None, max_consecutive_failures=None):
    return su.RateLimitedClient(
        "test-key",
        max_calls=max_calls,
        max_consecutive_failures=max_consecutive_failures,
        call_log_path=tmp_path / "call_log.jsonl",
        sleep=clock.sleep,
        clock=clock,
    )


def run_fetch(monkeypatch, tmp_path, clock, ids, *, statements_by_id=None, max_calls=None, max_consecutive_failures=None, **client_kwargs):
    fake = FakeSimFin(monkeypatch, statements=statements_by_id or {ids_i: statements_record(ids_i) for ids_i in ids})
    client = make_client(tmp_path, clock, max_calls=max_calls, max_consecutive_failures=max_consecutive_failures)
    stats = sf.FundamentalsStats()
    outcomes = sf.fetch_fundamentals(client, ids, cache_dir=tmp_path / "cache", stats=stats, **client_kwargs)
    return fake, client, stats, outcomes


# --- build_batches -------------------------------------------------------------


def test_build_batches_pairs_consecutive_ids_preserving_order():
    assert sf.build_batches([1, 2, 3, 4]) == [[1, 2], [3, 4]]


def test_build_batches_last_group_may_hold_one_id():
    assert sf.build_batches([1, 2, 3]) == [[1, 2], [3]]


def test_build_batches_of_an_empty_list_is_empty():
    assert sf.build_batches([]) == []


# --- ids_to_fetch ----------------------------------------------------------------


def test_ids_to_fetch_skips_ids_with_a_valid_cache_file_and_keeps_order(tmp_path):
    cache_dir = tmp_path / "cache"
    sf._write_envelope(
        sf.cache_path(cache_dir, 2),
        {"kind": "fundamentals_raw", "simfin_id": 2, "statements": list(sf.DEFAULT_STATEMENTS), "outcome": "ok",
         "fetched_at_utc": "x", "rate_limit_remaining": None, "response": statements_record(2)},
    )
    stats = sf.FundamentalsStats()
    assert sf.ids_to_fetch([1, 2, 3], cache_dir, stats=stats) == [1, 3]
    assert stats.cache_invalid == 0


def test_ids_to_fetch_refetches_when_the_statements_tuple_differs_from_the_cached_envelope(tmp_path):
    cache_dir = tmp_path / "cache"
    sf._write_envelope(
        sf.cache_path(cache_dir, 1),
        {"kind": "fundamentals_raw", "simfin_id": 1, "statements": ["PL"], "outcome": "ok",
         "fetched_at_utc": "x", "rate_limit_remaining": None, "response": statements_record(1)},
    )
    stats = sf.FundamentalsStats()
    assert sf.ids_to_fetch([1], cache_dir, statements=("PL", "BS", "CF"), stats=stats) == [1]
    assert stats.cache_invalid == 1


def test_ids_to_fetch_refetches_and_counts_a_corrupt_cache_file(tmp_path):
    cache_dir = tmp_path / "cache"
    path = sf.cache_path(cache_dir, 1)
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not json", encoding="utf-8")
    stats = sf.FundamentalsStats()
    assert sf.ids_to_fetch([1], cache_dir, stats=stats) == [1]
    assert stats.cache_invalid == 1


def test_ids_to_fetch_refetches_a_file_holding_a_different_ids_envelope(tmp_path):
    cache_dir = tmp_path / "cache"
    sf._write_envelope(
        sf.cache_path(cache_dir, 1),
        {"kind": "fundamentals_raw", "simfin_id": 999, "statements": list(sf.DEFAULT_STATEMENTS), "outcome": "ok",
         "fetched_at_utc": "x", "rate_limit_remaining": None, "response": statements_record(999)},
    )
    stats = sf.FundamentalsStats()
    assert sf.ids_to_fetch([1], cache_dir, stats=stats) == [1]
    assert stats.cache_invalid == 1


# --- fetch_fundamentals: happy path, caching, resumability ---------------------


def test_fetch_fundamentals_writes_one_cache_file_per_id_not_per_batch(monkeypatch, tmp_path, clock):
    fake, client, stats, outcomes = run_fetch(monkeypatch, tmp_path, clock, [1, 2])
    assert outcomes == {1: sf.FETCH_OK, 2: sf.FETCH_OK}
    assert fake.statements_requests() == [[1, 2]]
    assert stats.batches_total == 1 and stats.ids_ok == 2
    for simfin_id in (1, 2):
        envelope = json.loads(sf.cache_path(tmp_path / "cache", simfin_id).read_text(encoding="utf-8"))
        assert envelope["simfin_id"] == simfin_id and envelope["outcome"] == sf.FETCH_OK
        assert envelope["response"]["id"] == simfin_id


def test_fetch_fundamentals_matches_records_by_id_not_position(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, statements={1: statements_record(1), 2: statements_record(2)})
    fake.shuffle = True
    client = make_client(tmp_path, clock)
    stats = sf.FundamentalsStats()
    outcomes = sf.fetch_fundamentals(client, [1, 2], cache_dir=tmp_path / "cache", stats=stats)
    assert outcomes == {1: sf.FETCH_OK, 2: sf.FETCH_OK}
    envelope_1 = json.loads(sf.cache_path(tmp_path / "cache", 1).read_text(encoding="utf-8"))
    assert envelope_1["response"]["id"] == 1


def test_a_second_call_reuses_every_cached_id_and_makes_no_request(monkeypatch, tmp_path, clock):
    fake, client, stats, outcomes = run_fetch(monkeypatch, tmp_path, clock, [1, 2])
    stats2 = sf.FundamentalsStats()
    outcomes2 = sf.fetch_fundamentals(client, [1, 2], cache_dir=tmp_path / "cache", stats=stats2)
    assert outcomes2 == {1: sf.FETCH_CACHED, 2: sf.FETCH_CACHED}
    assert fake.statements_requests() == [[1, 2]]  # no new request
    assert stats2.batches_total == 0 and stats2.ids_already_cached == 2


def test_resuming_after_a_partial_run_only_fetches_the_still_missing_ids(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, statements={1: statements_record(1), 2: statements_record(2), 3: statements_record(3)})
    client = make_client(tmp_path, clock, max_calls=1)
    stats = sf.FundamentalsStats()
    outcomes = sf.fetch_fundamentals(client, [1, 2, 3], cache_dir=tmp_path / "cache", stats=stats)
    assert outcomes[1] == sf.FETCH_OK and outcomes[2] == sf.FETCH_OK and outcomes[3] == sf.FETCH_NOT_FETCHED

    client2 = make_client(tmp_path, clock)
    stats2 = sf.FundamentalsStats()
    outcomes2 = sf.fetch_fundamentals(client2, [1, 2, 3], cache_dir=tmp_path / "cache", stats=stats2)
    assert outcomes2 == {1: sf.FETCH_CACHED, 2: sf.FETCH_CACHED, 3: sf.FETCH_OK}
    assert fake.statements_requests() == [[1, 2], [3]]  # only the missing id's batch went out


def test_a_batch_with_only_one_remaining_id_is_requested_alone_and_cached(monkeypatch, tmp_path, clock):
    fake, client, stats, outcomes = run_fetch(monkeypatch, tmp_path, clock, [1])
    assert outcomes == {1: sf.FETCH_OK}
    assert fake.statements_requests() == [[1]]


def test_fetch_fundamentals_covers_every_requested_id_exactly_once(monkeypatch, tmp_path, clock):
    ids = list(range(1, 8))
    fake, client, stats, outcomes = run_fetch(monkeypatch, tmp_path, clock, ids)
    assert set(outcomes) == set(ids)


def test_fetch_fundamentals_rejects_duplicate_input_ids(tmp_path, clock):
    client = make_client(tmp_path, clock)
    stats = sf.FundamentalsStats()
    with pytest.raises(ValueError, match="unique"):
        sf.fetch_fundamentals(client, [1, 1], cache_dir=tmp_path / "cache", stats=stats)


def test_the_rate_limit_header_is_stored_in_each_ids_envelope(monkeypatch, tmp_path, clock):
    fake, client, stats, outcomes = run_fetch(monkeypatch, tmp_path, clock, [1])
    envelope = json.loads(sf.cache_path(tmp_path / "cache", 1).read_text(encoding="utf-8"))
    assert envelope["rate_limit_remaining"] == "0"  # FakeSimFin's default header


# --- absent ids (no requery logic) ----------------------------------------------


def test_an_id_absent_from_an_otherwise_valid_response_is_cached_as_absent_and_not_refetched(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, statements={1: statements_record(1)})  # id 2 has no data
    client = make_client(tmp_path, clock)
    stats = sf.FundamentalsStats()
    outcomes = sf.fetch_fundamentals(client, [1, 2], cache_dir=tmp_path / "cache", stats=stats)
    assert outcomes == {1: sf.FETCH_OK, 2: sf.FETCH_ABSENT}
    assert stats.ids_absent == 1
    envelope = json.loads(sf.cache_path(tmp_path / "cache", 2).read_text(encoding="utf-8"))
    assert envelope["outcome"] == sf.FETCH_ABSENT and envelope["response"] is None

    # a rerun must not re-request the absent id either
    client2 = make_client(tmp_path, clock)
    stats2 = sf.FundamentalsStats()
    outcomes2 = sf.fetch_fundamentals(client2, [1, 2], cache_dir=tmp_path / "cache", stats=stats2)
    assert outcomes2 == {1: sf.FETCH_CACHED, 2: sf.FETCH_CACHED}
    assert fake.statements_requests() == [[1, 2]]  # no second request


# --- whole-response contract violations -----------------------------------------


def test_a_response_with_an_unrequested_id_fails_the_whole_batch_and_is_not_cached(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, statements={1: statements_record(1), 2: statements_record(2)})
    fake.extra_id_in_batch = 99  # SimFin returns an id that was never requested
    client = make_client(tmp_path, clock)
    stats = sf.FundamentalsStats()
    outcomes = sf.fetch_fundamentals(client, [1, 2], cache_dir=tmp_path / "cache", stats=stats)
    assert outcomes == {1: sf.FETCH_FAILED, 2: sf.FETCH_FAILED}
    assert stats.batches_failed == 1
    assert not sf.cache_path(tmp_path / "cache", 1).exists()


def test_an_unrequested_id_in_the_response_is_rejected():
    with pytest.raises(sf.StatementsResponseError, match="not requested"):
        sf._parse_statements_response([statements_record(99)], [1])


def test_a_duplicate_id_in_the_response_is_rejected():
    with pytest.raises(sf.StatementsResponseError, match="twice"):
        sf._parse_statements_response([statements_record(1), statements_record(1)], [1])


def test_a_response_that_is_not_a_list_is_rejected():
    with pytest.raises(sf.StatementsResponseError, match="expected a list"):
        sf._parse_statements_response({"not": "a list"}, [1])


def test_a_non_integer_id_in_the_response_is_rejected():
    with pytest.raises(sf.StatementsResponseError, match="integer id"):
        sf._parse_statements_response([{"id": "1"}], [1])


def test_a_non_object_record_in_the_response_is_rejected():
    with pytest.raises(sf.StatementsResponseError, match="non-object"):
        sf._parse_statements_response(["not an object"], [1])


def test_statements_response_error_is_a_value_error_so_get_json_treats_it_like_any_other_bad_response():
    assert issubclass(sf.StatementsResponseError, ValueError)


def test_invalid_responses_count_toward_the_consecutive_failures_abort(monkeypatch, tmp_path, clock):
    # every batch's response includes an id that was never requested - a
    # contract violation, isolating each batch AND counting toward the
    # circuit breaker (docs/decisions.md "Phase 2.0b": "a response violating
    # the contract" is a failure kind, same as simfin_universe.py's prices).
    ids = [1, 2, 3, 4]
    fake = FakeSimFin(monkeypatch, statements={i: statements_record(i) for i in ids})
    fake.extra_id_in_batch = 99
    client = make_client(tmp_path, clock, max_consecutive_failures=2)
    stats = sf.FundamentalsStats()
    outcomes = sf.fetch_fundamentals(client, ids, cache_dir=tmp_path / "cache", stats=stats)
    assert client.failure_abort is not None and "invalid_response x2" in client.failure_abort
    assert outcomes[1] == sf.FETCH_FAILED and outcomes[2] == sf.FETCH_FAILED
    assert outcomes[3] == sf.FETCH_NOT_FETCHED and outcomes[4] == sf.FETCH_NOT_FETCHED


# --- network/failure isolation --------------------------------------------------


def test_a_network_error_isolates_only_that_batch_and_a_rerun_retries_it(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, statements={1: statements_record(1), 2: statements_record(2), 3: statements_record(3)})
    fake.fail_calls = {1: urllib.error.URLError("connection refused")}
    client = make_client(tmp_path, clock)
    stats = sf.FundamentalsStats()
    outcomes = sf.fetch_fundamentals(client, [1, 2, 3], cache_dir=tmp_path / "cache", stats=stats)
    assert outcomes[1] == sf.FETCH_FAILED and outcomes[2] == sf.FETCH_FAILED and outcomes[3] == sf.FETCH_OK
    assert stats.batches_failed == 1
    assert not sf.cache_path(tmp_path / "cache", 1).exists()

    client2 = make_client(tmp_path, clock)
    stats2 = sf.FundamentalsStats()
    outcomes2 = sf.fetch_fundamentals(client2, [1, 2, 3], cache_dir=tmp_path / "cache", stats=stats2)
    assert outcomes2 == {1: sf.FETCH_OK, 2: sf.FETCH_OK, 3: sf.FETCH_CACHED}
    # first run: [1,2] failed, [3] succeeded; second run: only the failed batch is retried
    assert fake.statements_requests() == [[1, 2], [3], [1, 2]]


def test_the_call_budget_stops_further_batches_and_marks_the_rest_not_fetched(monkeypatch, tmp_path, clock):
    ids = [1, 2, 3, 4]
    fake = FakeSimFin(monkeypatch, statements={i: statements_record(i) for i in ids})
    client = make_client(tmp_path, clock, max_calls=1)
    stats = sf.FundamentalsStats()
    outcomes = sf.fetch_fundamentals(client, ids, cache_dir=tmp_path / "cache", stats=stats)
    assert outcomes == {1: sf.FETCH_OK, 2: sf.FETCH_OK, 3: sf.FETCH_NOT_FETCHED, 4: sf.FETCH_NOT_FETCHED}
    assert stats.batches_not_fetched == 1
    assert len(fake.statements_requests()) == 1  # no request attempted for the second batch either


def test_consecutive_failures_abort_stops_the_run_and_marks_the_rest_not_fetched(monkeypatch, tmp_path, clock):
    ids = [1, 2, 3, 4]
    fake = FakeSimFin(monkeypatch, statements={i: statements_record(i) for i in ids})
    fake.fail_calls = {n: 429 for n in range(1, 20)}
    client = make_client(tmp_path, clock, max_consecutive_failures=2)
    stats = sf.FundamentalsStats()
    outcomes = sf.fetch_fundamentals(client, ids, cache_dir=tmp_path / "cache", stats=stats)
    assert all(outcome == sf.FETCH_NOT_FETCHED for outcome in outcomes.values())
    assert client.failure_abort is not None
    assert stats.batches_not_fetched == 2  # both batches, none retried after the abort


def test_a_successful_batch_after_a_failed_one_resets_the_series(monkeypatch, tmp_path, clock):
    fake = FakeSimFin(monkeypatch, statements={2: statements_record(2)})  # id 1 has no scripted response
    fake.fail_calls = {1: 500}
    client = make_client(tmp_path, clock, max_consecutive_failures=5)
    stats = sf.FundamentalsStats()
    sf.fetch_fundamentals(client, [1], cache_dir=tmp_path / "cache", stats=stats)
    assert client.consecutive_failures == 1
    sf.fetch_fundamentals(client, [2], cache_dir=tmp_path / "cache", stats=stats)
    assert client.consecutive_failures == 0  # the second, successful batch reset it

"""SimFin fundamentals loader (Issue #43, docs/decisions.md "Phase 2.0b"): a
resumable, rate-limited raw-data cache for companies/statements/compact,
independent of any universe pre-filter status - one raw response per SimFin
id, cached under <cache_dir>/<simfin_id>.json (cache_dir already carries the
<as_of> segment, added by the caller, run_fundamentals.py).

Reuses simfin_universe.RateLimitedClient as-is: its call budget, request
spacing, 429 retry and consecutive-failure circuit breaker are already
endpoint-agnostic (nothing in the class is prices/listing-specific).
simfin_client.fetch_statements_batch() is deliberately NOT used here - its
own independent 12-attempt/90s retry loop has no budget/log/circuit-breaker
awareness, and nesting it inside a RateLimitedClient-driven loop would defeat
the call budget. _write_envelope/_read_envelope/_now_utc are duplicated from
simfin_universe.py rather than imported: small, private helpers: duplicating
keeps this module independent of future changes to that module's envelope
format.

Data-integrity rules (CLAUDE.md, docs/decisions.md "Phase 2.0b"):
- companies/statements/compact serves at most MAX_BATCH_SIZE (2) ids per
  call (simfin_client.py); 3+ fails with HTTP 500 "Exceeded number of
  companies allowed" - reconfirmed live on 2026-09-25.
- Unlike prices (fetch_prices), a live probe on 2026-09-25 found zero ids
  silently dropped from an otherwise contract-valid MAX_BATCH_SIZE response:
  a company id simply absent from a structurally valid response is cached as
  "absent" and never re-queried. Confirmed at n=2 only, not at the full
  1.821-id scale (docs/decisions.md "Phase 2.0b" Entscheidung 2).
- A response that violates the contract as a whole (not a list, a record
  that is not an object, a non-integer id, an id that was not requested, or
  an id that occurs twice) fails the whole batch and is not cached, so a
  rerun retries it.
- A rate limit or an outage is not one bad batch: after
  max_consecutive_failures failed requests in a row without a successful
  response in between, the client raises ConsecutiveFailuresAbort and makes
  no further request; the remaining ids are "not_fetched" (already-cached
  ids are unaffected).
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from data_layer.simfin_client import MAX_BATCH_SIZE, SIMFIN_STATEMENTS_URL
from data_layer.simfin_universe import (
    CallBudgetExhausted,
    ConsecutiveFailuresAbort,
    NETWORK_ERRORS,
)

DEFAULT_STATEMENTS = ("PL", "BS", "CF")

# Per-id outcomes.
FETCH_OK = "ok"
FETCH_CACHED = "cached"
FETCH_ABSENT = "absent"
FETCH_FAILED = "fetch_failed"
FETCH_NOT_FETCHED = "not_fetched"


class StatementsResponseError(ValueError):
    """A companies/statements/compact response violates the contract as a
    whole (not a list, a record that is not an object, a non-integer id, an
    id that was not requested, or an id that occurs twice) - then it is
    unclear which record belongs to which company, so the whole batch fails
    and is not cached. An id simply absent from an otherwise valid response
    is NOT this - see FETCH_ABSENT."""


@dataclass
class FundamentalsStats:
    """Counters for the run report - nothing here is a threshold."""

    batches_total: int = 0
    batches_network: int = 0
    batches_failed: int = 0
    batches_not_fetched: int = 0
    ids_ok: int = 0
    ids_absent: int = 0
    ids_already_cached: int = 0
    cache_invalid: int = 0
    errors: list = field(default_factory=list)  # (context, message) of isolated failures
    # Full (MAX_BATCH_SIZE-sized) batches where the response was structurally
    # valid (HTTP 200, contract intact) but at least one requested id was
    # still absent - the one signal that would indicate a silent cap like the
    # one fetch_prices guards against (docs/decisions.md "Phase 2.0b"
    # Entscheidung 2: confirmed absent at a single 2-id probe, not at scale).
    # A batch smaller than MAX_BATCH_SIZE (the possible trailing batch of
    # size 1) is never appended here: a lone id simply missing says nothing
    # about pairing-related drops. {"batch": [...], "missing": [...]}
    partial_full_batches: list = field(default_factory=list)


def cache_path(cache_dir, simfin_id):
    return Path(cache_dir) / f"{simfin_id}.json"


def _write_envelope(path, envelope):
    """Atomic write (temp file + rename), duplicated from simfin_universe.py:
    a crash never leaves a half-written cache file a resumed run could
    mistake for a complete response."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_envelope(path, *, simfin_id, statements, stats):
    """The cached envelope for simfin_id, or None if absent, unreadable, or
    not for the statements tuple being asked for - all of which mean "fetch
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
        or envelope.get("kind") != "fundamentals_raw"
        or envelope.get("simfin_id") != simfin_id
        or envelope.get("statements") != list(statements)
        or envelope.get("outcome") not in (FETCH_OK, FETCH_ABSENT)
    ):
        stats.cache_invalid += 1
        return None
    return envelope


def _now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ids_to_fetch(survivor_ids, cache_dir, *, statements=DEFAULT_STATEMENTS, stats):
    """survivor_ids (in the given order) that do not already have a valid
    cache file for `statements` under cache_dir."""
    cache_dir = Path(cache_dir)
    return [
        simfin_id
        for simfin_id in survivor_ids
        if _read_envelope(cache_path(cache_dir, simfin_id), simfin_id=simfin_id, statements=statements, stats=stats)
        is None
    ]


def build_batches(ids):
    """Consecutive groups of at most MAX_BATCH_SIZE ids, preserving order;
    the last group may be smaller (odd count -> a final group of size 1)."""
    return [ids[i : i + MAX_BATCH_SIZE] for i in range(0, len(ids), MAX_BATCH_SIZE)]


def _parse_statements_response(response, requested_ids):
    """{id: raw record} for ids present in `response`. Raises
    StatementsResponseError if the RESPONSE as a whole violates the contract
    (see the module docstring); an id simply missing from an otherwise valid
    response is not an error - the caller treats it as FETCH_ABSENT."""
    if not isinstance(response, list):
        raise StatementsResponseError(f"statements response is {type(response).__name__}, expected a list")
    requested = set(requested_ids)
    records, seen = {}, set()
    for item in response:
        if not isinstance(item, dict):
            raise StatementsResponseError("statements response contains a non-object record")
        company_id = item.get("id")
        if isinstance(company_id, bool) or not isinstance(company_id, int):
            raise StatementsResponseError(f"statements record without an integer id: {company_id!r}")
        if company_id not in requested:
            raise StatementsResponseError(f"statements response contains id {company_id} that was not requested")
        if company_id in seen:
            raise StatementsResponseError(f"statements response contains id {company_id} twice")
        seen.add(company_id)
        records[company_id] = item
    return records


def _fetch_batch(client, batch, *, statements):
    """One statements call for `batch` (<= MAX_BATCH_SIZE ids) -> ({id: raw
    record}, rate_limit_remaining). Raises CallBudgetExhausted,
    ConsecutiveFailuresAbort or a NETWORK_ERRORS member (StatementsResponseError
    included, via record_failure); never caches - that's the caller's job."""
    url = f"{SIMFIN_STATEMENTS_URL}?id={','.join(str(i) for i in batch)}&statements={','.join(statements)}"
    response, remaining = client.get_json(url, endpoint="statements/compact", n_ids=len(batch))
    try:
        records = _parse_statements_response(response, batch)
    except StatementsResponseError as exc:
        client.record_failure("invalid_response", exc)  # raises ConsecutiveFailuresAbort at the limit
        raise
    client.record_success()
    return records, remaining


def fetch_fundamentals(client, survivor_ids, *, cache_dir, statements=DEFAULT_STATEMENTS, stats, progress=None):
    """Fetch and cache raw companies/statements/compact data for every id in
    survivor_ids not already validly cached under cache_dir - one file per
    id, independent of any pre-filter status. Resumable: batches are built
    fresh each call from only the still-missing ids (see ids_to_fetch), so a
    rerun finds every already-cached id for free and only pays for the rest.

    Returns {id: outcome} covering every id in survivor_ids exactly once,
    outcome one of FETCH_OK / FETCH_CACHED / FETCH_ABSENT / FETCH_FAILED /
    FETCH_NOT_FETCHED. Never raises for a per-batch problem (isolated into
    stats/outcomes): once CallBudgetExhausted or ConsecutiveFailuresAbort is
    seen, every remaining batch is marked FETCH_NOT_FETCHED without another
    request.
    """
    survivor_ids = list(survivor_ids)
    if len(set(survivor_ids)) != len(survivor_ids):
        raise ValueError("fetch_fundamentals requires unique ids")
    cache_dir = Path(cache_dir)

    missing = ids_to_fetch(survivor_ids, cache_dir, statements=statements, stats=stats)
    missing_set = set(missing)
    outcomes = {}
    for simfin_id in survivor_ids:
        if simfin_id not in missing_set:
            stats.ids_already_cached += 1
            outcomes[simfin_id] = FETCH_CACHED

    batches = build_batches(missing)
    aborted = False
    for number, batch in enumerate(batches, start=1):
        stats.batches_total += 1
        if aborted:
            stats.batches_not_fetched += 1
            for simfin_id in batch:
                outcomes[simfin_id] = FETCH_NOT_FETCHED
        else:
            try:
                records, remaining = _fetch_batch(client, batch, statements=statements)
            except (CallBudgetExhausted, ConsecutiveFailuresAbort):
                aborted = True
                stats.batches_not_fetched += 1
                for simfin_id in batch:
                    outcomes[simfin_id] = FETCH_NOT_FETCHED
            except NETWORK_ERRORS as exc:
                stats.batches_failed += 1
                stats.errors.append((f"batch {batch}", f"{type(exc).__name__}: {exc}"))
                for simfin_id in batch:
                    outcomes[simfin_id] = FETCH_FAILED
            else:
                stats.batches_network += 1
                missing_in_batch = []
                for simfin_id in batch:
                    if simfin_id in records:
                        outcome, response_value = FETCH_OK, records[simfin_id]
                        stats.ids_ok += 1
                    else:
                        outcome, response_value = FETCH_ABSENT, None
                        stats.ids_absent += 1
                        missing_in_batch.append(simfin_id)
                    _write_envelope(
                        cache_path(cache_dir, simfin_id),
                        {
                            "kind": "fundamentals_raw",
                            "simfin_id": simfin_id,
                            "statements": list(statements),
                            "outcome": outcome,
                            "fetched_at_utc": _now_utc(),
                            "rate_limit_remaining": remaining,
                            "response": response_value,
                        },
                    )
                    outcomes[simfin_id] = outcome
                if len(batch) == MAX_BATCH_SIZE and missing_in_batch:
                    stats.partial_full_batches.append({"batch": list(batch), "missing": missing_in_batch})
        if progress is not None:
            progress(number, len(batches))

    return outcomes

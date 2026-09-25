"""Fundamentals cache runner (Issue #43, docs/decisions.md "Phase 2.0b"):

    python run_fundamentals.py [--max-calls N] [--run-timestamp TS] ...

Fetches and caches raw companies/statements/compact data (PL/BS/CF) for the
survivors of a universe pre-filter run (data/run_archive.db,
universe_prefilter, status='passed'), one raw response per SimFin id under
data/cache/fundamentals/<as_of>/ (resumable: already-cached ids are skipped
on a rerun). Raw cache only - no mapping onto internal metrics, no
run_stage1.py integration, no new run_archive.db table: those are a
deliberately separate, later step (docs/decisions.md "Phase 2.0b" Kontext).

Reuses simfin_universe.RateLimitedClient (call budget, request spacing, 429
retry, consecutive-failure circuit breaker, JSONL call log) and
run_stage1.resolve_api_key() unchanged; run_stage1.py, simfin_client.py,
simfin_universe.py and store_layer/db.py are not modified by this runner.

--as-of defaults to the date of the resolved --run-timestamp (the survivors
snapshot), not today: fundamentals are not day-sensitive the way prices are,
and a multi-day backfill must resume into the same cache directory instead
of silently starting a fresh empty one at midnight (docs/decisions.md
"Phase 2.0b" Entscheidung 5).

Failure guard, same semantics as run_universe.py's --max-consecutive-failures:
after that many failed requests in a row (HTTP 429, 5xx or any other HTTP
error, timeout, network error, unreadable or contract-violating response)
without a successful response in between, the run stops making requests
(consecutive_failures_abort). The ids not yet fetched stay "not_fetched";
already-cached ids are unaffected; the cache stays for a resume.

Exit code 0 = the run was carried out and cached, whatever the result (also
when incomplete because of --max-calls or a failed batch, which the summary
states as INCOMPLETE); non-zero (1) = a technical failure that stopped the
run before anything was cached (no API key, no universe_prefilter run with
any passed row, invalid --max-consecutive-failures, cache dir not writable).
"""

import argparse
import sqlite3
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT / "src"))  # same fixup as run_stage1.py/run_universe.py

from data_layer import simfin_fundamentals as sf  # noqa: E402
from data_layer import simfin_universe as su  # noqa: E402
from run_stage1 import Stage1RunError, resolve_api_key  # noqa: E402
from store_layer.db import DEFAULT_DB_PATH  # noqa: E402

DEFAULT_CACHE_DIR = _REPO_ROOT / "data" / "cache" / "fundamentals"

EXIT_OK = 0
EXIT_TECHNICAL_FAILURE = 1

PROGRESS_EVERY_BATCHES = 50
# Operating parameter of the run (CLI --max-consecutive-failures), not a
# business threshold - same meaning/wording as run_universe.py's flag.
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5


class FundamentalsRunError(Exception):
    """A technical failure that aborts the whole run before anything is
    cached - never a single id's or batch's problem, which is isolated
    instead (fetch_failed/not_fetched)."""


def load_survivor_ids(db_path, run_timestamp=None):
    """(resolved run_timestamp, [(simfin_id, ticker), ...] ascending by
    simfin_id) from universe_prefilter where status='passed'.

    run_timestamp=None resolves to the latest run_timestamp that has at
    least one passed row. Raises FundamentalsRunError if the database is
    missing/unreadable or no run_timestamp with a passed row exists
    (explicit or latest). A local, read-only query, not a store_layer/db.py
    addition: keeps this module's diff at zero lines in any file
    run_stage1.py depends on (docs/decisions.md "Phase 2.0b" Entscheidung 4).
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FundamentalsRunError(f"run archive not found at {db_path}")
    try:
        connection = sqlite3.connect(db_path)
        try:
            if run_timestamp is None:
                row = connection.execute(
                    "SELECT run_timestamp FROM universe_prefilter WHERE status = 'passed' "
                    "ORDER BY run_timestamp DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    raise FundamentalsRunError(f"no universe_prefilter run with any passed row in {db_path}")
                run_timestamp = row[0]
            rows = connection.execute(
                "SELECT simfin_id, ticker FROM universe_prefilter WHERE run_timestamp = ? AND status = 'passed' "
                "ORDER BY simfin_id",
                (run_timestamp,),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.OperationalError as exc:
        raise FundamentalsRunError(f"run archive not readable at {db_path}: {exc}") from exc
    if not rows:
        raise FundamentalsRunError(f"run_timestamp {run_timestamp!r} has no passed rows in {db_path}")
    return run_timestamp, rows


def _print_summary(out, *, run_timestamp, as_of, rows, outcomes, stats, client, complete, duration, full_batch_checks):
    statuses = Counter(outcomes.values())
    abort = "  abort_reason=consecutive_failures_abort" if client.failure_abort else ""
    print(f"run_timestamp={run_timestamp}  as_of={as_of}  complete={complete}{abort}", file=out)
    print(f"survivors={len(rows)}  calls={client.calls}  http_429={client.http_429}  duration_s={duration:.0f}", file=out)
    print(
        f"batches: total={stats.batches_total} network={stats.batches_network} "
        f"failed={stats.batches_failed} not_fetched={stats.batches_not_fetched} | cache_invalid={stats.cache_invalid}",
        file=out,
    )
    print(
        f"ids: ok={statuses[sf.FETCH_OK]} absent={statuses[sf.FETCH_ABSENT]} cached={statuses[sf.FETCH_CACHED]} "
        f"fetch_failed={statuses[sf.FETCH_FAILED]} not_fetched={statuses[sf.FETCH_NOT_FETCHED]}",
        file=out,
    )
    # "absent" alone does not distinguish "genuinely no data" from a silent
    # cap like the one fetch_prices guards against (docs/decisions.md "Phase
    # 2.0b" Entscheidung 2 is confirmed at a single 2-id probe, not at
    # scale): check every full MAX_BATCH_SIZE batch explicitly, not just the
    # aggregate absent count. Computed from the PERSISTED, cumulative log
    # (read_full_batch_checks), not from this process's own stats, so a
    # resumed backfill's earlier runs are not silently dropped from the count
    # (docs/decisions.md "Phase 2.0b" Entscheidung 2, Review-Nachtrag).
    gaps = [entry for entry in full_batch_checks if entry["missing"]]
    if gaps:
        print(
            f"ACHTUNG: {len(gaps)} von {len(full_batch_checks)} vollen {sf.MAX_BATCH_SIZE}er-Batches (ueber "
            "alle bisherigen Laeufe mit diesem --run-timestamp/--as-of) enthielten eine fehlende ID trotz "
            "erfolgreicher Antwort - moeglicher Hinweis auf eine stille Kappung wie bei den Preisen. Vor dem "
            "Vollauf einzeln pruefen:",
            file=out,
        )
        for entry in gaps[:10]:
            print(f"  batch={entry['batch']} missing={entry['missing']}", file=out)
        if len(gaps) > 10:
            print(f"  ... und {len(gaps) - 10} weitere", file=out)
    else:
        print(
            f"voller-Batch-Pruefung: 0 von {len(full_batch_checks)} vollen {sf.MAX_BATCH_SIZE}er-Batches (ueber "
            "alle bisherigen Laeufe mit diesem --run-timestamp/--as-of) mit fehlender ID (kein Hinweis auf eine "
            "stille Kappung).",
            file=out,
        )
    for context, message in stats.errors[:10]:
        print(f"error ({context}): {message}", file=out)
    if len(stats.errors) > 10:
        print(f"... and {len(stats.errors) - 10} more isolated errors", file=out)
    if complete:
        print("complete: every survivor id is cached (ok or absent).", file=out)
    elif client.failure_abort:
        print(
            f"INCOMPLETE ({client.failure_abort}): the ids not yet fetched are not_fetched; rerun with the "
            "same --run-timestamp/--as-of to resume (cached responses are skipped) once the cause has cleared.",
            file=out,
        )
    else:
        print(
            "INCOMPLETE: some ids were not fetched (budget or a failed batch); rerun to resume (cached "
            "responses are skipped).",
            file=out,
        )


def run(
    db_path=DEFAULT_DB_PATH,
    cache_dir=DEFAULT_CACHE_DIR,
    api_key=None,
    run_timestamp=None,
    as_of=None,
    max_calls=None,
    max_consecutive_failures=DEFAULT_MAX_CONSECUTIVE_FAILURES,
    sleep=time.sleep,
    clock=time.monotonic,
    out=sys.stdout,
):
    """One fundamentals cache run. Returns an exit code; never raises for a
    per-id or per-batch problem, only fails (exit 1) for a technical
    failure. sleep/clock are injectable so tests need not wait."""
    try:
        try:
            if api_key is None:
                api_key = resolve_api_key()
        except Stage1RunError as exc:
            raise FundamentalsRunError(str(exc)) from exc

        run_timestamp, rows = load_survivor_ids(db_path, run_timestamp)
        as_of = as_of or date.fromisoformat(run_timestamp[:10])
        run_cache_dir = Path(cache_dir) / as_of.isoformat()

        try:
            client = su.RateLimitedClient(
                api_key,
                max_calls=max_calls,
                max_consecutive_failures=max_consecutive_failures,
                call_log_path=run_cache_dir / "call_log.jsonl",
                sleep=sleep,
                clock=clock,
            )
        except ValueError as exc:  # an invalid --max-consecutive-failures (>= 1 required, no way to switch it off)
            raise FundamentalsRunError(str(exc)) from exc

        stats = sf.FundamentalsStats()
        survivor_ids = [simfin_id for simfin_id, _ in rows]
        call_log_path = run_cache_dir / "call_log.jsonl"

        def progress(done, total):
            if done % PROGRESS_EVERY_BATCHES == 0 or done == total:
                print(f"batches {done}/{total}  calls={client.calls}  http_429={client.http_429}", file=out, flush=True)

        try:
            outcomes = sf.fetch_fundamentals(
                client, survivor_ids, cache_dir=run_cache_dir, stats=stats, call_log_path=call_log_path, progress=progress
            )
        except OSError as exc:  # network errors are isolated per batch; what reaches here is the cache/log on disk
            raise FundamentalsRunError(f"fundamentals cache/log not writable: {exc}") from exc

        complete = all(outcome in (sf.FETCH_OK, sf.FETCH_CACHED, sf.FETCH_ABSENT) for outcome in outcomes.values())

        _print_summary(
            out,
            run_timestamp=run_timestamp,
            as_of=as_of,
            rows=rows,
            outcomes=outcomes,
            stats=stats,
            client=client,
            complete=complete,
            duration=clock() - client.started_at,
            full_batch_checks=sf.read_full_batch_checks(call_log_path),
        )
        return EXIT_OK
    except FundamentalsRunError as exc:
        print(f"run_fundamentals: technical failure, run aborted: {exc}", file=sys.stderr)
        return EXIT_TECHNICAL_FAILURE


def _non_negative_int(text):
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _positive_int(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def build_parser():
    parser = argparse.ArgumentParser(description="SimFin fundamentals raw-data cache (Issue #43).")
    parser.add_argument(
        "--run-timestamp",
        default=None,
        help="universe_prefilter run_timestamp to source survivor ids from (default: the latest run with any "
        "passed row)",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="cache-directory date, YYYY-MM-DD (default: the date of the resolved --run-timestamp, not today "
        "- docs/decisions.md 'Phase 2.0b' Entscheidung 5)",
    )
    parser.add_argument(
        "--max-calls",
        type=_non_negative_int,
        default=None,
        help="budget of HTTP requests for this run; omit for no limit. Cached responses cost nothing.",
    )
    parser.add_argument(
        "--max-consecutive-failures",
        type=_positive_int,
        default=DEFAULT_MAX_CONSECUTIVE_FAILURES,
        help="stop the run cleanly after this many failed requests in a row (HTTP 429, 5xx or any other HTTP "
        "error, timeout, network error, unreadable or contract-violating response) without a successful "
        f"response in between (default {DEFAULT_MAX_CONSECUTIVE_FAILURES}): consecutive_failures_abort; the "
        "ids not yet fetched stay not_fetched, the cache stays for a resume.",
    )
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return run(
        db_path=args.db_path,
        cache_dir=args.cache_dir,
        run_timestamp=args.run_timestamp,
        as_of=args.as_of,
        max_calls=args.max_calls,
        max_consecutive_failures=args.max_consecutive_failures,
    )


if __name__ == "__main__":
    sys.exit(main())

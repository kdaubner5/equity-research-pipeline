"""Universe pre-filter runner (Issue #39, docs/decisions.md "Phase 2.0a"):

    python run_universe.py [--max-calls N] [--as-of YYYY-MM-DD] ...

Loads the SimFin US universe (general/compact bulk, cross-checked against
companies/list), fetches each classified company's price history in batches
(resumable: responses are cached under data/cache/universe/<as_of>/ and
skipped on a rerun), applies the pre-filter (market cap / median dollar volume
/ price freshness, thresholds from config/thresholds.yaml `prefilter`), and
archives EVERY US company - passed and excluded, with the computed values -
in the run archive's universe_prefilter table.

The passed companies are written to data/universe/survivors_<timestamp>.txt
(one ticker per line, the format of config/universe.txt) - only for a
complete run. config/universe.txt itself and the Stage-1 behaviour
(run_stage1.py) are not touched.

A separate runner, deliberately not part of run_stage1.py: different data
(prices for the whole universe, hundreds of calls), different schedule.

Failure model mirrors run_stage1.py (docs/decisions.md, Issue #9
Entscheidungen 2-3): a failing price batch never aborts the run - its
companies are archived as price_fetch_failed and a rerun retries just that
batch. Exit code 0 = the run was carried out and archived, whatever the
result (also when incomplete because of --max-calls or failed batches, which
the summary states as INCOMPLETE, or with 0 survivors); non-zero (1) = a
technical failure that stopped the run before anything was written (no API
key, invalid config, universe listing unavailable or unusable, duplicate
tickers among the candidates, cache/archive not writable).
"""

import argparse
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT / "src"))  # same fixup as run_stage1.py

from config_layer.thresholds import DEFAULT_THRESHOLDS_PATH  # noqa: E402
from data_layer import simfin_universe as su  # noqa: E402
from gates_layer import universe_prefilter as pf  # noqa: E402
from run_stage1 import Stage1RunError, resolve_api_key, resolve_thresholds  # noqa: E402
from store_layer.db import (  # noqa: E402
    DEFAULT_DB_PATH,
    DEFAULT_UNIVERSE_DIR,
    export_universe_survivors,
    save_universe_prefilter,
)

DEFAULT_CACHE_DIR = _REPO_ROOT / "data" / "cache" / "universe"

EXIT_OK = 0
EXIT_TECHNICAL_FAILURE = 1

PROGRESS_EVERY_BATCHES = 50


class UniverseRunError(Exception):
    """A technical failure that aborts the whole run - never one company's
    data problem, which is archived with a reason instead."""


def load_priority_tickers(path):
    """One ticker per line, blank lines skipped (the universe.txt format)."""
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def _order_ids(candidates, priority_tickers):
    """Candidate ids: priority tickers first (in the given order), then the
    rest ascending by id. Returns (ordered ids, priority tickers not found)."""
    by_ticker = {entry.ticker.upper(): entry.simfin_id for entry in candidates}
    priority_ids, missing = [], []
    for ticker in priority_tickers or ():
        simfin_id = by_ticker.get(ticker.upper())
        if simfin_id is None:
            missing.append(ticker)
        elif simfin_id not in priority_ids:
            priority_ids.append(simfin_id)
    prioritized = set(priority_ids)
    rest = sorted(entry.simfin_id for entry in candidates if entry.simfin_id not in prioritized)
    return priority_ids + rest, missing


def build_results(snapshot, outcomes, *, as_of, thresholds):
    """One PrefilterResult per US company in the snapshot - classified or
    not, priced or not - so nothing is silently skipped."""
    results = []
    for entry in snapshot.entries:
        if not entry.has_classification:
            results.append(pf.evaluate_prefilter(entry, None, as_of=as_of, thresholds=thresholds.prefilter))
            continue
        outcome = outcomes.get(entry.simfin_id)
        if outcome is None:
            raise UniverseRunError(f"internal error: no price outcome for classified id {entry.simfin_id}")
        if outcome.status == su.PRICES_OK:
            results.append(pf.evaluate_prefilter(entry, outcome.rows, as_of=as_of, thresholds=thresholds.prefilter))
        elif outcome.status == su.PRICES_NO_DATA:
            results.append(pf.evaluate_prefilter(entry, None, as_of=as_of, thresholds=thresholds.prefilter))
        elif outcome.status == su.PRICES_FETCH_FAILED:
            results.append(pf.runner_excluded(entry, pf.PRICE_FETCH_FAILED))
        elif outcome.status == su.PRICES_NOT_FETCHED:
            results.append(pf.runner_excluded(entry, pf.NOT_FETCHED))
        else:
            raise UniverseRunError(f"internal error: unknown price outcome {outcome.status!r}")
    return results


def _print_summary(out, *, run_timestamp, as_of, snapshot, results, stats, client, complete, survivors_path, duration, priority_missing):
    reasons = Counter(result.reason for result in results if result.reason is not None)
    statuses = Counter(result.status for result in results)
    classified = len(snapshot.classified())
    check = snapshot.crosscheck
    print(f"run_timestamp={run_timestamp}  as_of={as_of}  complete={complete}", file=out)
    print(
        f"universe: listed={snapshot.total_listed} us={len(snapshot.entries)} non_us={snapshot.non_us} "
        f"without_id={snapshot.without_id} classified={classified} unclassified={len(snapshot.entries) - classified} "
        f"| list_vs_general: only_in_list={check.only_in_list} only_in_general={check.only_in_general} "
        f"field_mismatches={check.field_mismatches}",
        file=out,
    )
    print(
        f"calls={client.calls} http_429={client.http_429} duration_s={duration:.0f} "
        f"rate_limit_header_values={sorted(client.rate_limit_values_seen)}",
        file=out,
    )
    print(
        f"batches: total={stats.batches_total} cached={stats.batches_cached} network={stats.batches_network} "
        f"failed={stats.batches_failed} not_fetched={stats.batches_not_fetched} | requeries: "
        f"network={stats.requeries_network} cached={stats.requeries_cached} with_data={stats.requery_with_data} "
        f"empty={stats.requery_empty} failed={stats.requery_failed} not_fetched={stats.requery_not_fetched} "
        f"| cache_invalid={stats.cache_invalid} non_usd={stats.non_usd}",
        file=out,
    )
    print(f"status: passed={statuses[pf.PASSED]} excluded={statuses[pf.EXCLUDED]}", file=out)
    print("reasons: " + " ".join(f"{reason}={reasons[reason]}" for reason in pf.PREFILTER_REASONS if reasons[reason]), file=out)
    if priority_missing:
        print(f"priority tickers not found among the classified candidates: {priority_missing}", file=out)
    for context, message in stats.errors[:10]:
        print(f"error ({context}): {message}", file=out)
    if len(stats.errors) > 10:
        print(f"... and {len(stats.errors) - 10} more isolated errors", file=out)
    if complete:
        print(f"survivors_file={survivors_path}", file=out)
    else:
        print(
            "INCOMPLETE: some companies were not fetched (budget or failed batch) and are archived as "
            "not_fetched/price_fetch_failed; rerun to resume (cached responses are skipped). No survivors file written.",
            file=out,
        )


def run(
    db_path=DEFAULT_DB_PATH,
    cache_dir=DEFAULT_CACHE_DIR,
    survivors_dir=DEFAULT_UNIVERSE_DIR,
    api_key=None,
    as_of=None,
    max_calls=None,
    run_timestamp=None,
    thresholds_path=DEFAULT_THRESHOLDS_PATH,
    priority_tickers=None,
    sleep=time.sleep,
    clock=time.monotonic,
    out=sys.stdout,
):
    """One universe pre-filter run. Returns an exit code; never raises for a
    per-company or per-batch problem, only fails (exit 1) for a technical
    failure. `sleep`/`clock` are injectable so tests need not wait."""
    try:
        try:
            if api_key is None:
                api_key = resolve_api_key()
            thresholds, config_hash = resolve_thresholds(thresholds_path)
        except Stage1RunError as exc:
            raise UniverseRunError(str(exc)) from exc
        as_of = as_of or date.today()
        run_cache_dir = Path(cache_dir) / as_of.isoformat()
        stats = su.LoaderStats()
        client = su.RateLimitedClient(
            api_key, max_calls=max_calls, call_log_path=run_cache_dir / "call_log.jsonl", sleep=sleep, clock=clock
        )

        try:
            snapshot = su.load_universe(client, run_cache_dir, stats=stats)
        except (su.CallBudgetExhausted, su.UniverseDataError, OSError, ValueError) as exc:
            # OSError also covers urllib's URLError/HTTPError/TimeoutError and a
            # non-writable cache directory; ValueError covers a JSON decode error.
            raise UniverseRunError(f"universe listing unavailable: {type(exc).__name__}: {exc}") from exc
        duplicates, blanks = snapshot.problem_tickers()
        if duplicates or blanks:
            raise UniverseRunError(
                f"classified US entries with a duplicate ticker {duplicates[:10]} or no ticker "
                f"(ids {blanks[:10]}) - the survivors file would be ambiguous"
            )

        candidates = snapshot.classified()
        ordered_ids, priority_missing = _order_ids(candidates, priority_tickers)

        def progress(done, total):
            if done % PROGRESS_EVERY_BATCHES == 0 or done == total:
                print(f"batches {done}/{total}  calls={client.calls}  http_429={client.http_429}", file=out, flush=True)

        try:
            outcomes = su.fetch_prices(
                client,
                ordered_ids,
                as_of=as_of,
                adv_window_trading_days=thresholds.prefilter.adv_window_trading_days,
                run_cache_dir=run_cache_dir,
                stats=stats,
                progress=progress,
            )
        except OSError as exc:  # network errors are isolated per batch; what reaches here is the cache/log on disk
            raise UniverseRunError(f"price cache/log not writable: {exc}") from exc

        results = build_results(snapshot, outcomes, as_of=as_of, thresholds=thresholds)
        complete = not any(result.reason in pf.RUNNER_REASONS for result in results)
        run_timestamp = run_timestamp or datetime.now().isoformat(timespec="seconds")

        # Survivors file first, archive last (as run_stage1.py does): the DB
        # write is the append-only authoritative record, so it happens only
        # once everything else is known-good; if it then fails, the file is
        # removed again, keeping "non-zero exit code" = "nothing written".
        survivors_path = None
        try:
            if complete:
                survivors_path = export_universe_survivors(run_timestamp, results, output_dir=survivors_dir)
            save_universe_prefilter(run_timestamp, results, config_hash=config_hash, db_path=db_path)
        except Exception as exc:
            if survivors_path is not None:
                survivors_path.unlink(missing_ok=True)
            raise UniverseRunError(f"could not write run results: {type(exc).__name__}: {exc}") from exc

        _print_summary(
            out,
            run_timestamp=run_timestamp,
            as_of=as_of,
            snapshot=snapshot,
            results=results,
            stats=stats,
            client=client,
            complete=complete,
            survivors_path=survivors_path,
            duration=clock() - client.started_at,
            priority_missing=priority_missing,
        )
        return EXIT_OK
    except UniverseRunError as exc:
        print(f"run_universe: technical failure, run aborted: {exc}", file=sys.stderr)
        return EXIT_TECHNICAL_FAILURE


def _non_negative_int(text):
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def build_parser():
    parser = argparse.ArgumentParser(description="SimFin universe pre-filter (Issue #39).")
    parser.add_argument(
        "--max-calls",
        type=_non_negative_int,
        default=None,
        help="budget of HTTP requests for this run (every request counts, incl. 429 retries and single-id "
        "re-queries); omit for no limit. Cached responses cost nothing.",
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=None, help="price date reference, YYYY-MM-DD (default: today)")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--survivors-dir", type=Path, default=DEFAULT_UNIVERSE_DIR)
    parser.add_argument(
        "--priority-tickers-file",
        type=Path,
        default=None,
        help="file with one ticker per line that are fetched before all others",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    priority = load_priority_tickers(args.priority_tickers_file) if args.priority_tickers_file else None
    return run(
        db_path=args.db_path,
        cache_dir=args.cache_dir,
        survivors_dir=args.survivors_dir,
        as_of=args.as_of,
        max_calls=args.max_calls,
        priority_tickers=priority,
    )


if __name__ == "__main__":
    sys.exit(main())

"""Trend-Guard (Masterplan Stufe 1.1, Hebel 3 aus Issue #52) over the fundamentals cache
(docs/decisions.md "Trend-Guard (Masterplan Stufe 1.1, Hebel 3 aus Issue #52)"):

    python run_trend_guard.py --db-path ... --archive-db-path ... --csv-dir ... \\
        --fundamentals-cache-dir ... --stage1-run-timestamp TS [--prefilter-run-timestamp TS] [--as-of YYYY-MM-DD]
        [--stage2-results-db-path ... --stage2-run-timestamp TS]

A separate step after Stage 2 (decision Q1): it marks, never excludes, and changes no Stage-1 or Stage-2
status, flag count or rank (Q2). 0 SimFin calls, no network. Reads (read-only, SQLite mode=ro) the passed
rows of one universe_prefilter run (to map ticker -> simfin_id) and ALL survivors of one archived Stage-1
run (Q6: all of them, also those that fail Stage 2), reads each survivor's raw statements from the
fundamentals cache and evaluates the four signals with gates_layer.trend_guard. Results go to the own
tables trend_runs/trend_results of --archive-db-path and to trend_guard_<ts>.csv.

No path has a default: the real data/run_archive.db is never opened or written by accident, only when it
is named on the command line (which needs an explicit instruction, CLAUDE.md).

Nothing is skipped silently. A survivor whose statements are missing, corrupt, unmappable or malformed, or
that is absent from the pre-filter run, gets a trend_not_evaluable result with its own reason. A technical
failure (unreadable archive or config, duplicate tickers, unknown run, write failure) aborts with exit 1
before anything is written, or removes the CSV again if the archive write fails.

Optional --stage2-results-db-path plus --stage2-run-timestamp (both or neither) read the Stage-2 result
table read-only and add a report block for the companies that passed Stage 2 (hypotheses H-T7).

Exit code 0 = the run completed and wrote its results, whatever the outcome per company;
1 = technical failure, nothing written.
"""

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT / "src"))  # same fixup as the other runners

from config_layer.stage2 import DEFAULT_STAGE2_PATH, Stage2ConfigError, load_stage2_config  # noqa: E402
from config_layer.thresholds import DEFAULT_THRESHOLDS_PATH, ThresholdsConfigError, load_thresholds  # noqa: E402
from config_layer.trend_guard import (  # noqa: E402
    DEFAULT_TREND_GUARD_PATH,
    TrendGuardConfigError,
    compute_trend_guard_config_hash,
    load_trend_guard_config,
)
from data_layer import simfin_fundamentals as sf  # noqa: E402
from data_layer.simfin_client import SimFinMappingError, build_company_record  # noqa: E402
from gates_layer.trend_guard import (  # noqa: E402
    MARK_ORDER,
    STATUS_CLEAR,
    STATUS_MARKED,
    STATUS_NOT_EVALUABLE,
    evaluate_trend,
    not_evaluable_trend_result,
)
from metrics_layer.result import MetricInputError  # noqa: E402
from store_layer.trend_store import export_trend_csv, save_trend_results  # noqa: E402

import run_stage2  # noqa: E402  (reuses its read-only archive readers; it puts src/ on the path itself)

EXIT_OK = 0
EXIT_TECHNICAL_FAILURE = 1

FUNDAMENTALS_CACHE_MISSING = "fundamentals_cache_missing"
FUNDAMENTALS_CACHE_INVALID = "fundamentals_cache_invalid"
SIMFIN_MAPPING_ERROR = "simfin_mapping_error"
FUNDAMENTALS_MALFORMED = "fundamentals_malformed"
NOT_IN_UNIVERSE_PREFILTER = "not_in_universe_prefilter"

_NUMERIC_KEYS = ("fiscal_year", "revenue", "ebit", "ebitda", "equity", "total_assets")
_STAGE2_PASSED = ("passed", "passed_with_flags")


class TrendRunError(Exception):
    """A technical failure that aborts the whole run - never one company's data problem."""


def _periods_are_well_formed(periods):
    """True if every value the Trend-Guard reads is a real number that fits a float (net_debt may be None
    = unreliable). NaN/inf are numbers here; the metrics turn them into non_finite_input."""
    for p in periods:
        if not all(run_stage2._is_number(p[k]) for k in _NUMERIC_KEYS):
            return False
        if p["net_debt"] is not None and not run_stage2._is_number(p["net_debt"]):
            return False
    return True


def build_periods(ticker, simfin_id, *, cache_dir, reconciliation, max_years):
    """(periods, None) or (None, problem reason) for one survivor."""
    try:
        raw_company = sf.read_cached_statements(cache_dir, simfin_id, statements=sf.DEFAULT_STATEMENTS)
    except sf.CachedStatementsError:
        return None, FUNDAMENTALS_CACHE_INVALID
    if raw_company is None:
        return None, FUNDAMENTALS_CACHE_MISSING
    try:
        periods = build_company_record(ticker, raw_company, thresholds=reconciliation, max_years=max_years)["periods"]
    except SimFinMappingError:
        return None, SIMFIN_MAPPING_ERROR
    except (TypeError, KeyError, IndexError, AttributeError, ValueError, OverflowError):
        return None, FUNDAMENTALS_MALFORMED
    if not _periods_are_well_formed(periods):
        return None, FUNDAMENTALS_MALFORMED
    return periods, None


def load_stage2_passers(results_db_path, stage2_run_timestamp):
    """Tickers that passed Stage 2 (status passed / passed_with_flags) in one stage2_results run, read-only.
    An unknown run is a mistake and aborts."""
    try:
        connection = run_stage2._connect_read_only(results_db_path)
        try:
            if connection.execute("SELECT 1 FROM stage2_runs WHERE run_timestamp = ?", (stage2_run_timestamp,)).fetchone() is None:
                raise TrendRunError(f"Stage-2 run {stage2_run_timestamp!r} not found in {results_db_path} (table stage2_runs)")
            rows = connection.execute(
                "SELECT ticker FROM stage2_results WHERE run_timestamp = ? AND status IN (?, ?)",
                (stage2_run_timestamp, *_STAGE2_PASSED),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise TrendRunError(f"could not read the Stage-2 results from {results_db_path}: {exc}") from exc
    except run_stage2.Stage2RunError as exc:
        raise TrendRunError(str(exc)) from exc
    return sorted(ticker for (ticker,) in rows)


def run(
    db_path,
    archive_db_path,
    csv_dir,
    fundamentals_cache_dir,
    stage1_run_timestamp,
    prefilter_run_timestamp=None,
    as_of=None,
    run_timestamp=None,
    thresholds_path=DEFAULT_THRESHOLDS_PATH,
    trend_config_path=DEFAULT_TREND_GUARD_PATH,
    stage2_config_path=DEFAULT_STAGE2_PATH,
    stage2_results_db_path=None,
    stage2_run_timestamp=None,
    out=None,
):
    """One Trend-Guard run. Returns an exit code; raises nothing for a per-company problem."""
    out = out if out is not None else sys.stdout  # resolved per call, so a redirected stdout is honoured
    try:
        if (stage2_results_db_path is None) != (stage2_run_timestamp is None):
            raise TrendRunError("--stage2-results-db-path and --stage2-run-timestamp must be given together or not at all")
        try:
            config = load_trend_guard_config(trend_config_path)
            config_hash = compute_trend_guard_config_hash(trend_config_path)
        except TrendGuardConfigError as exc:
            raise TrendRunError(f"trend guard config invalid: {exc}") from exc
        try:
            # Q8: only the Stage-2 value fundamentals.max_fiscal_year_lag is read, the file is not changed
            max_fiscal_year_lag = load_stage2_config(stage2_config_path).fundamentals.max_fiscal_year_lag
        except Stage2ConfigError as exc:
            raise TrendRunError(f"stage2 config invalid: {exc}") from exc
        try:
            thresholds = load_thresholds(thresholds_path)  # roic (T3) and data_quality_reconciliation are used
        except ThresholdsConfigError as exc:
            raise TrendRunError(f"thresholds config invalid: {exc}") from exc

        try:
            prefilter_ts, rows = run_stage2.load_prefilter_rows(db_path, prefilter_run_timestamp)
            survivors = run_stage2.load_stage1_survivors(db_path, stage1_run_timestamp)
        except run_stage2.Stage2RunError as exc:
            raise TrendRunError(str(exc)) from exc

        by_ticker = {}
        for simfin_id, ticker, _, _ in rows:
            if ticker is not None:
                by_ticker.setdefault(ticker, []).append(simfin_id)
        duplicates = {t: ids for t, ids in by_ticker.items() if len(ids) > 1}
        if duplicates:
            raise TrendRunError(
                f"universe_prefilter run {prefilter_ts!r} has ticker(s) shared by several simfin_ids "
                f"(ticker is the key of the results): {duplicates}"
            )
        try:
            as_of = as_of or date.fromisoformat(prefilter_ts[:10])
        except ValueError as exc:
            raise TrendRunError(f"universe_prefilter run_timestamp {prefilter_ts!r} does not start with a date: {exc}") from exc
        cache_dir = Path(fundamentals_cache_dir) / as_of.isoformat()

        passers = None if stage2_results_db_path is None else load_stage2_passers(stage2_results_db_path, stage2_run_timestamp)

        results, problems, simfin_ids = {}, {}, {}
        for ticker in sorted(survivors):
            ids = by_ticker.get(ticker)
            if not ids:
                problems[ticker] = NOT_IN_UNIVERSE_PREFILTER
                results[ticker] = not_evaluable_trend_result(NOT_IN_UNIVERSE_PREFILTER)
                continue
            simfin_ids[ticker] = ids[0]
            periods, problem = build_periods(
                ticker, ids[0], cache_dir=cache_dir, reconciliation=thresholds.data_quality_reconciliation,
                max_years=config.years,
            )
            if problem is not None:
                problems[ticker] = problem
                results[ticker] = not_evaluable_trend_result(problem)
                continue
            try:
                results[ticker] = evaluate_trend(
                    periods, as_of_year=as_of.year, config=config, roic_thresholds=thresholds.roic,
                    max_fiscal_year_lag=max_fiscal_year_lag,
                )
            except MetricInputError as exc:  # a caller-side data shape bug, not a company's data problem
                raise TrendRunError(f"Trend-Guard evaluation failed on malformed input for {ticker}: {exc}") from exc

        run_timestamp = run_timestamp or datetime.now().isoformat(timespec="seconds")
        csv_path = None
        try:
            csv_path = export_trend_csv(run_timestamp, results, output_dir=csv_dir)
            save_trend_results(
                run_timestamp, results=results, stage1_run_timestamp=stage1_run_timestamp,
                prefilter_run_timestamp=prefilter_ts, as_of=as_of, config_hash=config_hash, simfin_ids=simfin_ids,
                db_path=archive_db_path,
            )
        except Exception as exc:
            if csv_path is not None:
                csv_path.unlink(missing_ok=True)
            raise TrendRunError(f"could not write Trend-Guard results: {exc}") from exc

        _print_summary(
            out, run_timestamp=run_timestamp, prefilter_ts=prefilter_ts, stage1_run_timestamp=stage1_run_timestamp,
            as_of=as_of, csv_path=csv_path, cache_dir=cache_dir, config_hash=config_hash, results=results,
            problems=problems, passers=passers, stage2_run_timestamp=stage2_run_timestamp,
        )
        return EXIT_OK
    except TrendRunError as exc:
        print(f"run_trend_guard: technical failure, run aborted: {exc}", file=sys.stderr)
        return EXIT_TECHNICAL_FAILURE


def _status_line(label, results):
    statuses = Counter(r.status for r in results.values())
    return f"{label}: " + "  ".join(f"{s}={statuses[s]}" for s in (STATUS_CLEAR, STATUS_MARKED, STATUS_NOT_EVALUABLE))


def _marks_line(label, results):
    marks = Counter(m for r in results.values() for m in r.marks)
    return f"{label}: " + "  ".join(f"{m}={marks[m]}" for m in MARK_ORDER)


def _print_summary(
    out, *, run_timestamp, prefilter_ts, stage1_run_timestamp, as_of, csv_path, cache_dir, config_hash, results, problems,
    passers, stage2_run_timestamp,
):
    n = len(results)
    print(
        f"run_timestamp={run_timestamp}  stage1_run_timestamp={stage1_run_timestamp}  "
        f"prefilter_run_timestamp={prefilter_ts}  as_of={as_of}  trend_config_hash={config_hash[:12]}",
        file=out,
    )
    print(f"trend_guard_csv={csv_path}  fundamentals_cache_dir={cache_dir}", file=out)
    print(f"survivors_evaluated={n}", file=out)
    print(_status_line(f"status (von {n} Ueberlebenden)", results), file=out)
    print(_marks_line(f"marken (Firmen, von {n} Ueberlebenden)", results), file=out)
    print(f"mit_mindestens_einer_marke: {sum(1 for r in results.values() if r.mark_count)} von {n} Ueberlebenden", file=out)
    reasons = Counter(
        reason
        for r in results.values()
        for reason in {r.revenue_invalid_reason, r.margin_invalid_reason, r.roic_invalid_reason}
        if reason is not None
    )
    if reasons:
        print(
            f"nicht berechenbar (Firmen, von {n} Ueberlebenden): " + "  ".join(f"{k}={v}" for k, v in sorted(reasons.items())),
            file=out,
        )
    if problems:
        print(
            f"fundamentals unusable for {len(problems)} company(ies): "
            + "  ".join(f"{k}={v}" for k, v in sorted(Counter(problems.values()).items())),
            file=out,
        )
    if passers is not None:
        in_results = [t for t in passers if t in results]
        subset = {t: results[t] for t in in_results}
        print(
            f"stage2_bestanden (Stage-2-Lauf {stage2_run_timestamp}): {len(passers)} Firmen, davon {len(in_results)} in diesem Lauf "
            f"(ohne Ergebnis: {sorted(set(passers) - set(in_results))})",
            file=out,
        )
        print(_status_line(f"status (von {len(in_results)} Stage-2-Bestandenen)", subset), file=out)
        print(_marks_line(f"marken (Firmen, von {len(in_results)} Stage-2-Bestandenen)", subset), file=out)
        for ticker in in_results:
            if subset[ticker].mark_count:
                print(f"  markiert: {ticker}: {','.join(subset[ticker].marks)}", file=out)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Trend-Guard (informative trend marks for the Stage-1 survivors) over the fundamentals cache, "
        "0 SimFin calls. No path has a default: name every database and directory explicitly."
    )
    parser.add_argument("--db-path", type=Path, required=True, help="run archive to READ from (read-only): universe_prefilter and run_results")
    parser.add_argument("--archive-db-path", type=Path, required=True, help="run archive to WRITE trend_runs/trend_results to")
    parser.add_argument("--csv-dir", type=Path, required=True)
    parser.add_argument("--fundamentals-cache-dir", type=Path, required=True, help="base directory of the fundamentals cache; the --as-of date is appended")
    parser.add_argument("--stage1-run-timestamp", required=True, help="run_timestamp of the archived Stage-1 run whose survivors are evaluated")
    parser.add_argument("--prefilter-run-timestamp", default=None, help="universe_prefilter run (default: the latest with a passed row)")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None, help="cache-directory date (default: the date of the prefilter run)")
    parser.add_argument("--run-timestamp", default=None, help="archive key of this run (default: now); a repeated value is refused")
    parser.add_argument("--thresholds-path", type=Path, default=DEFAULT_THRESHOLDS_PATH)
    parser.add_argument("--trend-config-path", type=Path, default=DEFAULT_TREND_GUARD_PATH)
    parser.add_argument("--stage2-config-path", type=Path, default=DEFAULT_STAGE2_PATH, help="only fundamentals.max_fiscal_year_lag is read")
    parser.add_argument("--stage2-results-db-path", type=Path, default=None, help="archive holding stage2_results (read-only); optional, needs --stage2-run-timestamp")
    parser.add_argument("--stage2-run-timestamp", default=None, help="Stage-2 run whose passers get an extra report block; optional, needs --stage2-results-db-path")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return run(
        db_path=args.db_path,
        archive_db_path=args.archive_db_path,
        csv_dir=args.csv_dir,
        fundamentals_cache_dir=args.fundamentals_cache_dir,
        stage1_run_timestamp=args.stage1_run_timestamp,
        prefilter_run_timestamp=args.prefilter_run_timestamp,
        as_of=args.as_of,
        run_timestamp=args.run_timestamp,
        thresholds_path=args.thresholds_path,
        trend_config_path=args.trend_config_path,
        stage2_config_path=args.stage2_config_path,
        stage2_results_db_path=args.stage2_results_db_path,
        stage2_run_timestamp=args.stage2_run_timestamp,
    )


if __name__ == "__main__":
    sys.exit(main())

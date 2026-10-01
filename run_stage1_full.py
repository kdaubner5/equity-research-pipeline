"""Stage-1 over the fundamentals cache, full universe (Issue #45,
docs/decisions.md "Phase 2.0c"):

    python run_stage1_full.py [--run-timestamp TS] [--as-of YYYY-MM-DD] ...

Wires the existing, unmodified Stage-1 evaluation logic
(run_stage1.evaluate_ticker -> pipeline.evaluate_company ->
gates_layer.evaluate_stage1_gates) onto the raw-fundamentals cache
run_fundamentals.py already populated (data/cache/fundamentals/<as_of>/) -
0 new SimFin calls, no network access at all. Survivors (simfin_id, ticker)
come from the existing run_fundamentals.load_survivor_ids(); results are
archived into the same run_results/eliminated_results tables the 23-pilot
run_stage1.py runs already use (store_layer.db.save_run_results/
export_survivors_csv, unchanged).

A missing cache entry or one cached with outcome=="absent" gets the same
elimination reason the live path uses for an empty batch response,
SIMFIN_STATEMENTS_MISSING; a cache file that exists but is corrupt/invalid
gets its own, distinguishable reason, SIMFIN_STATEMENTS_CACHE_INVALID
(docs/decisions.md "Phase 2.0c", 2026-09-30) - never silently skipped
(CLAUDE.md data-integrity convention). A survivor with no ticker
(universe_prefilter.ticker is nullable) is eliminated as TICKER_MISSING and
excluded from the archive (which requires a non-null ticker), reported in
the run's summary output, not silently dropped. Two survivors sharing one
ticker (universe_prefilter has no ticker-uniqueness constraint, unlike the
archive's (run_timestamp, ticker) key) abort the whole run rather than
silently overwrite one with the other (review fix, 2026-09-30).

Before any evaluation the run checks every survivor's sectorCode
(universe_prefilter.sector_code of the chosen run_timestamp, read-only)
against config/sector_profiles.yaml (docs/decisions.md "Phase 2.0e", Issue
#51): a code that is not listed, or NULL, aborts the whole run naming the
missing codes and writes nothing - an incomplete config, not a data problem
of the company. A survivor whose profile is excluded (banks, insurers,
REITs, biotech) gets the status "unsupported_business_model" without being
evaluated; every other result carries its profile and the borderline_sector
marker (informational only). run_stage1.py, the pilot runner, applies no
profile check.

--run-timestamp selects only the universe_prefilter run survivors are
sourced from (same meaning as run_fundamentals.py's identically-named
flag); the archive run_timestamp this run writes under is generated fresh
at call time (docs/decisions.md "Phase 2.0c", Nachtrag zu Entscheidung 2,
2026-09-30).

Exit code 0 = the run completed and wrote its results, regardless of how
many companies survived; non-zero only for a technical failure that
prevented the run from producing results at all (survivors archive
missing/unreadable, invalid thresholds file, the store layer failing to
write) - same contract as run_stage1.py/run_fundamentals.py.
"""

import argparse
import dataclasses
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT / "src"))  # same fixup as run_stage1.py/run_universe.py/run_fundamentals.py

from config_layer.sector_profiles import (  # noqa: E402
    DEFAULT_SECTOR_PROFILES_PATH,
    SectorProfilesConfigError,
    load_sector_profiles,
)
from data_layer import simfin_fundamentals as sf  # noqa: E402
from gates_layer.stage1_gates import GateResult, NOT_EVALUABLE, unsupported_business_model_result  # noqa: E402
from store_layer.db import DEFAULT_CSV_DIR, DEFAULT_DB_PATH, export_survivors_csv, save_run_results  # noqa: E402

from run_fundamentals import DEFAULT_CACHE_DIR as DEFAULT_FUNDAMENTALS_CACHE_DIR  # noqa: E402
from run_fundamentals import FundamentalsRunError, load_survivor_ids  # noqa: E402
from run_stage1 import SIMFIN_STATEMENTS_MISSING, Stage1RunError, evaluate_ticker, print_summary, resolve_thresholds  # noqa: E402
from config_layer.thresholds import DEFAULT_THRESHOLDS_PATH  # noqa: E402

EXIT_OK = 0
EXIT_TECHNICAL_FAILURE = 1

# Runner-level invalid_reason (docs/decisions.md "Phase 2.0c", 2026-09-30) -
# distinct from SIMFIN_STATEMENTS_MISSING: a cache file that exists but is
# corrupt/invalid is a local data problem, not a SimFin coverage gap.
SIMFIN_STATEMENTS_CACHE_INVALID = "simfin_statements_cache_invalid"

# A survivor row with no ticker at all (universe_prefilter.ticker is
# nullable) - distinct from both reasons above: there is no SimFin data
# problem here, the local record itself carries no ticker to evaluate or
# archive under (docs/decisions.md "Phase 2.0c", review fix, 2026-09-30).
TICKER_MISSING = "ticker_missing"


class Stage1FullRunError(Exception):
    """A technical failure that aborts the whole run - never a single
    company's data problem, which is caught and eliminated instead. Own
    type, matching this codebase's per-runner exception precedent
    (Stage1RunError, FundamentalsRunError, UniverseRunError are each
    independent, not shared)."""


def _eliminated(reason):
    """A GateResult for a ticker whose data could not even be read - no
    metric was ever computed, so all four raw values stay None. Duplicated
    from run_stage1._eliminated (private, so not imported across modules -
    same "small private helpers are duplicated" precedent this module's
    own read_cached_statements() already follows for _read_envelope)."""
    return GateResult(
        status=NOT_EVALUABLE,
        flags=(),
        invalid_reasons=(reason,),
        roic=None,
        net_debt_ebitda=None,
        cash_conversion=None,
        net_income_margin=None,
        data_quality_flags=(),
    )


def load_survivors(db_path, run_timestamp=None):
    """(resolved run_timestamp, [(simfin_id, ticker), ...] ascending by
    simfin_id) of the universe_prefilter survivors to evaluate - a thin
    delegate to the existing, tested run_fundamentals.load_survivor_ids()
    (docs/decisions.md "Phase 2.0c" Entscheidung 2: no second/different SQL
    query). Raises Stage1FullRunError (translated from FundamentalsRunError)
    if the archive is missing/unreadable, or no run_timestamp with a passed
    row exists (explicit or latest)."""
    try:
        return load_survivor_ids(db_path, run_timestamp)
    except FundamentalsRunError as exc:
        raise Stage1FullRunError(str(exc)) from exc


def load_survivor_sector_codes(db_path, source_run_timestamp):
    """{simfin_id: sector_code (or None)} of the passed rows of one universe_prefilter run.

    Read-only (SQLite mode=ro) and a local query like load_survivors()'s
    delegate - the archive is never written here. Raises Stage1FullRunError if
    it cannot be read.
    """
    try:
        # Path.as_uri() percent-encodes the path ('#', '%', spaces ...); a hand-built "file:..." URI
        # would treat '#' as the start of the fragment.
        connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return dict(
                connection.execute(
                    "SELECT simfin_id, sector_code FROM universe_prefilter WHERE run_timestamp = ? AND status = 'passed'",
                    (source_run_timestamp,),
                ).fetchall()
            )
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise Stage1FullRunError(f"could not read sector codes from {db_path}: {exc}") from exc


def check_sector_profile_coverage(rows, sector_codes, profiles, source_run_timestamp, profiles_path):
    """Abort (Stage1FullRunError) if any survivor's sectorCode is missing from the profile config.

    Names every missing code (and the survivors carrying a NULL code) and
    is called before any evaluation, so nothing has been written yet
    (docs/decisions.md "Phase 2.0e", Entscheidung 2).
    """
    unknown, null_count = profiles.find_uncovered(sector_codes.get(simfin_id) for simfin_id, _ in rows)
    if not unknown and not null_count:
        return
    problems = []
    if unknown:
        problems.append(f"sectorCode(s) not listed in {profiles_path}: {unknown}")
    if null_count:
        null_ids = [simfin_id for simfin_id, _ in rows if sector_codes.get(simfin_id) is None]
        problems.append(f"{null_count} survivor(s) with a NULL sectorCode (simfin_ids: {null_ids})")
    raise Stage1FullRunError(
        f"sector_profiles coverage incomplete for universe_prefilter run {source_run_timestamp!r} - "
        + "; ".join(problems)
        + ". Add the missing code(s) to the profile config; nothing was evaluated or written."
    )


def evaluate_from_cache(ticker, simfin_id, *, cache_dir, thresholds):
    """One survivor through the cache-sourced Stage-1 chain: read its
    cached raw statements and hand them to the exact same
    run_stage1.evaluate_ticker() the live path uses - a cached envelope's
    response is the identical dict shape as a live batch element, no
    transformation needed (docs/decisions.md "Phase 2.0c" Entscheidung 3).

    Never raises - every failure mode becomes an eliminated GateResult, one
    company's data problem is isolated, never aborts the run:
      - ticker is None (universe_prefilter.ticker is nullable): TICKER_MISSING,
        checked first - evaluate_ticker()/build_company_record() would
        otherwise crash on ticker.upper() when the cached response carries
        its own non-null ticker (review fix, 2026-09-30).
      - no cache file at all, or outcome=="absent": SIMFIN_STATEMENTS_MISSING
        (same reason the live path uses for an empty batch response).
      - a corrupt/unreadable/malformed cache file (CachedStatementsError):
        SIMFIN_STATEMENTS_CACHE_INVALID - kept distinguishable from the
        above (docs/decisions.md "Phase 2.0c", 2026-09-30).
      - anything else: delegated whole to run_stage1.evaluate_ticker(),
        which already isolates SimFinMappingError/MetricInputError.
    """
    if ticker is None:
        return _eliminated(TICKER_MISSING)
    try:
        raw_company = sf.read_cached_statements(cache_dir, simfin_id, statements=sf.DEFAULT_STATEMENTS)
    except sf.CachedStatementsError:
        return _eliminated(SIMFIN_STATEMENTS_CACHE_INVALID)
    if raw_company is None:
        return _eliminated(SIMFIN_STATEMENTS_MISSING)
    return evaluate_ticker(ticker, raw_company, thresholds=thresholds)


def run(
    db_path=DEFAULT_DB_PATH,
    archive_db_path=DEFAULT_DB_PATH,
    csv_dir=DEFAULT_CSV_DIR,
    fundamentals_cache_dir=DEFAULT_FUNDAMENTALS_CACHE_DIR,
    run_timestamp=None,
    as_of=None,
    thresholds_path=DEFAULT_THRESHOLDS_PATH,
    sector_profiles_path=DEFAULT_SECTOR_PROFILES_PATH,
    out=sys.stdout,
):
    """One full Stage-1 run over the fundamentals cache - 0 SimFin calls
    (docs/decisions.md "Phase 2.0c"). Returns an exit code; never raises for
    a per-company problem, only for a technical failure that stops the run
    before results could be written."""
    try:
        try:
            thresholds, config_hash = resolve_thresholds(thresholds_path)
        except Stage1RunError as exc:
            raise Stage1FullRunError(str(exc)) from exc
        try:
            profiles = load_sector_profiles(sector_profiles_path)
        except SectorProfilesConfigError as exc:
            raise Stage1FullRunError(f"sector profiles config invalid: {exc}") from exc

        source_run_timestamp, rows = load_survivors(db_path, run_timestamp)

        # run_results/eliminated_results key on ticker (PRIMARY KEY
        # (run_timestamp, ticker)) - universe_prefilter itself has no such
        # uniqueness constraint (its own key is (run_timestamp, simfin_id)),
        # so two survivors sharing a ticker cannot both be archived. A
        # {ticker: ...} dict comprehension would silently let the second
        # overwrite the first; refused loudly instead, before any
        # evaluation or archiving is attempted (review fix, 2026-09-30).
        by_ticker = {}
        for simfin_id, ticker in rows:
            if ticker is not None:
                by_ticker.setdefault(ticker, []).append(simfin_id)
        duplicates = {ticker: ids for ticker, ids in by_ticker.items() if len(ids) > 1}
        if duplicates:
            raise Stage1FullRunError(
                f"universe_prefilter run {source_run_timestamp!r} has {len(duplicates)} ticker(s) shared by "
                f"more than one simfin_id, which run_results/eliminated_results cannot both archive "
                f"(ticker is the archive key): {duplicates}"
            )

        # Coverage check BEFORE any evaluation or write (docs/decisions.md "Phase 2.0e",
        # Entscheidung 2): a code missing from the config aborts the run.
        sector_codes = load_survivor_sector_codes(db_path, source_run_timestamp)
        check_sector_profile_coverage(rows, sector_codes, profiles, source_run_timestamp, sector_profiles_path)

        as_of = as_of or date.fromisoformat(source_run_timestamp[:10])
        cache_dir = Path(fundamentals_cache_dir) / as_of.isoformat()

        # A row with no ticker at all is evaluated (evaluate_from_cache
        # eliminates it as TICKER_MISSING, never raises) but cannot be
        # archived either - run_results/eliminated_results.ticker is NOT
        # NULL. Excluded from gate_results, never silently: reported below
        # (review fix, 2026-09-30).
        gate_results = {}
        missing_ticker_ids = []
        for simfin_id, ticker in rows:
            assignment = profiles.assignment(sector_codes.get(simfin_id))
            if assignment is None:
                # check_sector_profile_coverage() above already aborts for any survivor without a listed
                # code, so this cannot happen today; it is only a safeguard (two separate queries feed
                # rows and sector_codes) that keeps "no silent skip" local to this loop.
                raise Stage1FullRunError(
                    f"no sector profile for survivor simfin_id {simfin_id} (sectorCode {sector_codes.get(simfin_id)!r}) "
                    f"- the coverage check should have aborted the run before any evaluation"
                )
            if not assignment.profile.supported:
                # excluded business model: not evaluated at all, so no cache read either
                result = unsupported_business_model_result(assignment.profile.name)
            else:
                result = evaluate_from_cache(ticker, simfin_id, cache_dir=cache_dir, thresholds=thresholds)
                result = dataclasses.replace(
                    result, profile=assignment.profile.name, borderline_sector=assignment.borderline
                )
            if ticker is None:
                missing_ticker_ids.append(simfin_id)
                continue
            gate_results[ticker] = result

        if missing_ticker_ids:
            print(
                f"ACHTUNG: {len(missing_ticker_ids)} Survivor(s) ohne Ticker koennen nicht archiviert werden "
                f"(run_results/eliminated_results verlangen einen Ticker) - simfin_ids: {missing_ticker_ids}",
                file=out,
            )

        # A fresh run_timestamp for THIS archive entry, distinct from the
        # universe_prefilter run_timestamp survivors were sourced from
        # (docs/decisions.md "Phase 2.0c", Nachtrag zu Entscheidung 2,
        # 2026-09-30) - same fallback run_stage1.run() uses when its own
        # run_timestamp argument is None.
        run_timestamp_archive = datetime.now().isoformat(timespec="seconds")
        # CSV first, DB write last, same rollback pattern as
        # run_stage1.run(): a nonzero exit code always means nothing was
        # written.
        csv_path = None
        try:
            csv_path = export_survivors_csv(run_timestamp_archive, gate_results, output_dir=csv_dir)
            save_run_results(run_timestamp_archive, gate_results, config_hash=config_hash, db_path=archive_db_path)
        except Exception as exc:
            if csv_path is not None:
                csv_path.unlink(missing_ok=True)
            raise Stage1FullRunError(f"could not write run results: {exc}") from exc

        print(
            f"source_run_timestamp={source_run_timestamp}  as_of={as_of}  "
            f"run_timestamp={run_timestamp_archive}  survivors_csv={csv_path}",
            file=out,
        )
        print_summary(gate_results, file=out)
        return EXIT_OK
    except Stage1FullRunError as exc:
        print(f"run_stage1_full: technical failure, run aborted: {exc}", file=sys.stderr)
        return EXIT_TECHNICAL_FAILURE


def build_parser():
    parser = argparse.ArgumentParser(
        description="Stage 1 over the full fundamentals-cache universe, 0 new SimFin calls (Issue #45)."
    )
    parser.add_argument(
        "--run-timestamp",
        default=None,
        help="universe_prefilter run_timestamp to source survivor (simfin_id, ticker) pairs from, via "
        "run_fundamentals.load_survivor_ids (default: the latest run with any passed row)",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="fundamentals cache-directory date, YYYY-MM-DD (default: the date of the resolved "
        "--run-timestamp, not today - same rationale as run_fundamentals.py's --as-of)",
    )
    parser.add_argument(
        "--fundamentals-cache-dir",
        type=Path,
        default=DEFAULT_FUNDAMENTALS_CACHE_DIR,
        help="base directory of the fundamentals raw-data cache written by run_fundamentals.py; the "
        "resolved --as-of date is appended (default: data/cache/fundamentals)",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="run archive to read survivor ids from (universe_prefilter table)",
    )
    parser.add_argument(
        "--archive-db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="run archive to write this run's results to (run_results/eliminated_results/runs); same "
        "file as --db-path by default, independently settable",
    )
    parser.add_argument("--csv-dir", type=Path, default=DEFAULT_CSV_DIR)
    parser.add_argument(
        "--sector-profiles-path",
        type=Path,
        default=DEFAULT_SECTOR_PROFILES_PATH,
        help="business-model profile config (sectorCode -> profile); a survivor sectorCode missing "
        "from it aborts the run before any evaluation (default: config/sector_profiles.yaml)",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return run(
        db_path=args.db_path,
        archive_db_path=args.archive_db_path,
        csv_dir=args.csv_dir,
        fundamentals_cache_dir=args.fundamentals_cache_dir,
        run_timestamp=args.run_timestamp,
        as_of=args.as_of,
        sector_profiles_path=args.sector_profiles_path,
    )


if __name__ == "__main__":
    sys.exit(main())

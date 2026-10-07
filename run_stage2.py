"""Stage 2 (relative valuation) over the fundamentals cache (docs/decisions.md "Stufe 2:
Peer-Gruppen und relative Bewertung" and "Stufe 2 - Beantwortung Q1-Q9 und Runner"):

    python run_stage2.py --db-path ... --archive-db-path ... --csv-dir ... \\
        --fundamentals-cache-dir ... --stage1-run-timestamp TS [--prefilter-run-timestamp TS] [--as-of YYYY-MM-DD]

0 SimFin calls, no network. Reads (read-only, SQLite mode=ro) the passed rows of one
universe_prefilter run (ticker, sector code, market cap) and the survivors of one archived
Stage-1 run (run_results), reads each company's raw statements from the fundamentals cache,
and evaluates exactly the Stage-1 survivors with gates_layer.stage2_gates. Results go to the
own tables stage2_runs/stage2_results of --archive-db-path and to stage2_survivors_<ts>.csv.

No path has a default: the real data/run_archive.db is never opened or written by accident,
only when it is named on the command line (which needs an explicit instruction, CLAUDE.md).

Nothing is skipped silently. A survivor whose statements are missing, corrupt or unmappable
gets a not_evaluable Stage-2 result with its own reason (fundamentals_cache_missing,
fundamentals_cache_invalid, simfin_mapping_error, fundamentals_malformed); a company without such data is simply not
a peer. A missing capex column makes only the FCF-yield invalid, a missing dividends or Other
Investing column only leaves that check unchecked (each *_unavailable is counted). Rows without a ticker, and rows of unsupported profiles, are counted in the
summary. A technical failure (unreadable archive or config, duplicate tickers, a sector code
missing from config/sector_profiles.yaml, unknown run, write failure) aborts with exit 1
before anything is written, or removes the CSV again if the archive write fails.

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

from config_layer.sector_profiles import (  # noqa: E402
    DEFAULT_SECTOR_PROFILES_PATH,
    SectorProfilesConfigError,
    load_sector_profiles,
)
from config_layer.stage2 import (  # noqa: E402
    DEFAULT_STAGE2_PATH,
    Stage2ConfigError,
    compute_stage2_config_hash,
    load_stage2_config,
)
from config_layer.thresholds import DEFAULT_THRESHOLDS_PATH, ThresholdsConfigError, load_thresholds  # noqa: E402
from data_layer import simfin_fundamentals as sf  # noqa: E402
from data_layer.simfin_client import SimFinMappingError, build_company_record  # noqa: E402
from data_layer.simfin_price_cache import PriceCacheError, load_price_cache_index  # noqa: E402
from data_layer.simfin_valuation import (  # noqa: E402
    extract_capex_by_fiscal_year,
    extract_dividends_paid_by_fiscal_year,
    extract_other_investing_by_fiscal_year,
)
from gates_layer.stage1_gates import GateResult  # noqa: E402
from gates_layer.stage2_gates import (  # noqa: E402
    FLAG_FCF_YIELD_UNVERIFIED,
    ValuationInput,
    assess_share_counts,
    evaluate_stage2,
    not_evaluable_result,
    peer_base_summary,
    rank_stage2,
)
from metrics_layer.plausibility import SHARE_AMBIGUOUS, SHARE_OK, SHARE_SUSPECT, SHARE_UNCHECKED  # noqa: E402
from metrics_layer.result import MetricInputError  # noqa: E402
from store_layer.stage2_store import export_stage2_csv, save_stage2_results  # noqa: E402

EXIT_OK = 0
EXIT_TECHNICAL_FAILURE = 1

FUNDAMENTALS_CACHE_MISSING = "fundamentals_cache_missing"
FUNDAMENTALS_CACHE_INVALID = "fundamentals_cache_invalid"
SIMFIN_MAPPING_ERROR = "simfin_mapping_error"
# Raw cells of the wrong type or rows of the wrong shape (a string where a number belongs, a row
# shorter than its column list ...): one company's data problem, never a reason to abort the run.
FUNDAMENTALS_MALFORMED = "fundamentals_malformed"
# A market cap of the wrong type in the archive (NULL stays a plain missing value handled by the metrics).
MARKET_CAP_MALFORMED = "market_cap_malformed"


class Stage2RunError(Exception):
    """A technical failure that aborts the whole run - never one company's data problem."""


def _connect_read_only(db_path):
    path = Path(db_path)
    if not path.exists():
        raise Stage2RunError(f"run archive not found at {path}")
    # Path.as_uri() percent-encodes '#', '%' and spaces; mode=ro never creates or writes the file.
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def load_prefilter_rows(db_path, prefilter_run_timestamp=None):
    """(resolved run_timestamp, [(simfin_id, ticker, sector_code, market_cap_usd), ...] by simfin_id)
    of the passed rows of one universe_prefilter run; None resolves to the latest run with a passed row."""
    try:
        connection = _connect_read_only(db_path)
        try:
            if prefilter_run_timestamp is None:
                row = connection.execute(
                    "SELECT run_timestamp FROM universe_prefilter WHERE status = 'passed' "
                    "ORDER BY run_timestamp DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    raise Stage2RunError(f"no universe_prefilter run with any passed row in {db_path}")
                prefilter_run_timestamp = row[0]
            rows = connection.execute(
                "SELECT simfin_id, ticker, sector_code, market_cap_usd FROM universe_prefilter "
                "WHERE run_timestamp = ? AND status = 'passed' ORDER BY simfin_id",
                (prefilter_run_timestamp,),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise Stage2RunError(f"could not read universe_prefilter from {db_path}: {exc}") from exc
    if not rows:
        raise Stage2RunError(f"universe_prefilter run {prefilter_run_timestamp!r} has no passed rows in {db_path}")
    return prefilter_run_timestamp, rows


def load_stage1_survivors(db_path, stage1_run_timestamp):
    """{ticker: GateResult} of the survivors (run_results) of one archived Stage-1 run.

    Only status and flags are archived for survivors, which is all the Stage-2 rank needs; the
    other GateResult fields are left None. The run must exist in `runs`: a 0-survivor run is a
    valid run, an unknown timestamp is a mistake and aborts.
    """
    try:
        connection = _connect_read_only(db_path)
        try:
            if connection.execute("SELECT 1 FROM runs WHERE run_timestamp = ?", (stage1_run_timestamp,)).fetchone() is None:
                raise Stage2RunError(f"Stage-1 run {stage1_run_timestamp!r} not found in {db_path} (table runs)")
            rows = connection.execute(
                "SELECT ticker, status, flags FROM run_results WHERE run_timestamp = ?", (stage1_run_timestamp,)
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise Stage2RunError(f"could not read the Stage-1 run from {db_path}: {exc}") from exc
    return {
        ticker: GateResult(
            status=status,
            flags=tuple(f for f in flags.split(",") if f),
            invalid_reasons=(),
            roic=None,
            net_debt_ebitda=None,
            cash_conversion=None,
            net_income_margin=None,
        )
        for ticker, status, flags in rows
    }


def _is_number(value):
    """A real number that fits a float (a JSON integer with 400 digits does not)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        float(value)
    except OverflowError:
        return False
    return True


def _periods_and_capex_are_well_formed(periods, capex_by_year, *extra_by_year):
    """True if every value the Stage-2 metrics read is a real number (None only where allowed):
    fiscal_year, ebitda, cfo always; net_debt may be None (unreliable); capex and the extra
    per-year maps (dividends paid, Other Investing) may hold None."""
    for p in periods:
        if not all(_is_number(p[k]) for k in ("fiscal_year", "ebitda", "cfo")):
            return False
        if p["net_debt"] is not None and not _is_number(p["net_debt"]):
            return False
    return all(v is None or _is_number(v) for by_year in (capex_by_year, *extra_by_year) for v in by_year.values())


def build_input(ticker, simfin_id, sector_code, market_cap_usd, assignment, *, cache_dir, reconciliation, price_entry=None):
    """(ValuationInput or None, problem or None, unavailable) for one pre-filtered company.

    An unsupported profile gets an input without periods and reads no cache. For a supported one
    the raw statements come from the cache: None (with a problem reason) if they are missing,
    invalid or unmappable - such a company is not a peer, and as a survivor it gets that reason.
    A cash-flow column that cannot be read (capex, dividends paid, Other Investing) only empties
    that map: the matching check turns invalid or unchecked, the company stays a peer; the names of
    such columns come back in `unavailable` (a set) and are counted in the report.
    price_entry (data_layer.simfin_price_cache.PriceCacheEntry or None) supplies the share count and
    the last dividend per share for the share-count check; None leaves them unset (unchecked).
    """
    if market_cap_usd is not None and not _is_number(market_cap_usd):
        return None, MARKET_CAP_MALFORMED, set()
    if not assignment.profile.supported:
        vi = ValuationInput(
            ticker=ticker, sector_code=sector_code, market_cap_usd=market_cap_usd, periods=[], capex_by_year={},
            profile_supported=False, borderline_sector=assignment.borderline,
        )
        return vi, None, set()
    try:
        raw_company = sf.read_cached_statements(cache_dir, simfin_id, statements=sf.DEFAULT_STATEMENTS)
    except sf.CachedStatementsError:
        return None, FUNDAMENTALS_CACHE_INVALID, set()
    if raw_company is None:
        return None, FUNDAMENTALS_CACHE_MISSING, set()
    try:
        periods = build_company_record(ticker, raw_company, thresholds=reconciliation)["periods"]
    except SimFinMappingError:
        return None, SIMFIN_MAPPING_ERROR, set()
    except (TypeError, KeyError, IndexError, AttributeError, ValueError, OverflowError):
        return None, FUNDAMENTALS_MALFORMED, set()
    unavailable = set()
    maps = {}
    for name, extract in (
        ("capex", extract_capex_by_fiscal_year),
        ("dividends", extract_dividends_paid_by_fiscal_year),
        ("other_investing", extract_other_investing_by_fiscal_year),
    ):
        try:
            maps[name] = extract(raw_company)
        except SimFinMappingError:
            maps[name] = {}
            unavailable.add(name)
        except (TypeError, KeyError, IndexError, AttributeError, ValueError, OverflowError):
            return None, FUNDAMENTALS_MALFORMED, set()
    if not _periods_and_capex_are_well_formed(periods, maps["capex"], maps["dividends"], maps["other_investing"]):
        return None, FUNDAMENTALS_MALFORMED, set()
    vi = ValuationInput(
        ticker=ticker, sector_code=sector_code, market_cap_usd=market_cap_usd, periods=periods,
        capex_by_year=maps["capex"], profile_supported=True, borderline_sector=assignment.borderline,
        dividends_paid_by_year=maps["dividends"], other_investing_by_year=maps["other_investing"],
        shares_outstanding=None if price_entry is None else price_entry.shares,
        dividend_per_share=None if price_entry is None else price_entry.dividend_per_share,
    )
    return vi, None, unavailable


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
    stage2_config_path=DEFAULT_STAGE2_PATH,
    sector_profiles_path=DEFAULT_SECTOR_PROFILES_PATH,
    universe_cache_dir=None,
    out=None,
):
    """One Stage-2 run. Returns an exit code; raises nothing for a per-company problem."""
    out = out if out is not None else sys.stdout  # resolved per call, so a redirected stdout is honoured
    try:
        try:
            config = load_stage2_config(stage2_config_path)
            config_hash = compute_stage2_config_hash(stage2_config_path)
        except Stage2ConfigError as exc:
            raise Stage2RunError(f"stage2 config invalid: {exc}") from exc
        try:
            thresholds = load_thresholds(thresholds_path)  # only its data_quality_reconciliation group is used
        except ThresholdsConfigError as exc:
            raise Stage2RunError(f"thresholds config invalid: {exc}") from exc
        try:
            profiles = load_sector_profiles(sector_profiles_path)
        except SectorProfilesConfigError as exc:
            raise Stage2RunError(f"sector profiles config invalid: {exc}") from exc

        prefilter_ts, rows = load_prefilter_rows(db_path, prefilter_run_timestamp)
        stage1_results = load_stage1_survivors(db_path, stage1_run_timestamp)

        by_ticker = {}
        for simfin_id, ticker, _, _ in rows:
            if ticker is not None:
                by_ticker.setdefault(ticker, []).append(simfin_id)
        duplicates = {t: ids for t, ids in by_ticker.items() if len(ids) > 1}
        if duplicates:
            raise Stage2RunError(
                f"universe_prefilter run {prefilter_ts!r} has ticker(s) shared by several simfin_ids "
                f"(ticker is the key of the Stage-2 inputs and results): {duplicates}"
            )

        # Coverage check before any work, like run_stage1_full: a sector code missing from the
        # profile config is an incomplete config, not a data problem of the company.
        unknown, null_count = profiles.find_uncovered(sector_code for _, _, sector_code, _ in rows)
        if unknown or null_count:
            raise Stage2RunError(
                f"sector_profiles coverage incomplete for universe_prefilter run {prefilter_ts!r}: "
                f"sectorCode(s) not listed in {sector_profiles_path}: {unknown}; rows with NULL sectorCode: {null_count}"
            )

        try:
            as_of = as_of or date.fromisoformat(prefilter_ts[:10])
        except ValueError as exc:
            raise Stage2RunError(f"universe_prefilter run_timestamp {prefilter_ts!r} does not start with a date: {exc}") from exc
        cache_dir = Path(fundamentals_cache_dir) / as_of.isoformat()

        # Share-count check input (docs/decisions.md "Stufe 2 - Datenplausibilitaet"): the universe run's own
        # price cache, read only. Without it every company is unchecked - said in the report, never assumed ok.
        price_index = None
        if universe_cache_dir is not None:
            try:
                price_index = load_price_cache_index(Path(universe_cache_dir) / as_of.isoformat() / "prices")
            except PriceCacheError as exc:
                raise Stage2RunError(str(exc)) from exc

        inputs, problems, simfin_ids = {}, {}, {}
        missing_ticker_ids, unavailable_columns, unsupported_rows = [], {}, 0
        for simfin_id, ticker, sector_code, market_cap in rows:
            if ticker is None:
                missing_ticker_ids.append(simfin_id)
                continue
            simfin_ids[ticker] = simfin_id
            assignment = profiles.assignment(sector_code)
            if not assignment.profile.supported:
                unsupported_rows += 1
            vi, problem, unavailable = build_input(
                ticker, simfin_id, sector_code, market_cap, assignment,
                cache_dir=cache_dir, reconciliation=thresholds.data_quality_reconciliation,
                price_entry=None if price_index is None else price_index.entries.get(simfin_id),
            )
            if problem is not None:
                problems[ticker] = problem
            else:
                inputs[ticker] = vi
            for column in unavailable:
                unavailable_columns.setdefault(column, []).append(ticker)

        try:
            results = evaluate_stage2(inputs, stage1_results, as_of=as_of, config=config)
            base_summary = peer_base_summary(inputs, as_of=as_of, config=config)
            assessments = assess_share_counts(inputs, config)
        except MetricInputError as exc:  # a caller-side data shape bug, not a company's data problem
            raise Stage2RunError(f"Stage-2 evaluation failed on malformed input: {exc}") from exc
        for ticker in list(results):
            if ticker in problems:
                results[ticker] = not_evaluable_result(problems[ticker])
        ranked = rank_stage2(stage1_results, results)

        run_timestamp = run_timestamp or datetime.now().isoformat(timespec="seconds")
        csv_path = None
        try:
            csv_path = export_stage2_csv(run_timestamp, ranked, output_dir=csv_dir)
            save_stage2_results(
                run_timestamp,
                results=results,
                ranked=ranked,
                stage1_run_timestamp=stage1_run_timestamp,
                prefilter_run_timestamp=prefilter_ts,
                as_of=as_of,
                config_hash=config_hash,
                peer_base_size=base_summary.size,
                simfin_ids=simfin_ids,
                db_path=archive_db_path,
            )
        except Exception as exc:
            if csv_path is not None:
                csv_path.unlink(missing_ok=True)
            raise Stage2RunError(f"could not write Stage-2 results: {exc}") from exc

        _print_summary(
            out, run_timestamp=run_timestamp, prefilter_ts=prefilter_ts, stage1_run_timestamp=stage1_run_timestamp,
            as_of=as_of, csv_path=csv_path, cache_dir=cache_dir, config_hash=config_hash, rows=rows, base_summary=base_summary, assessments=assessments, price_index=price_index, results=results,
            ranked=ranked, problems=problems, unsupported_rows=unsupported_rows, missing_ticker_ids=missing_ticker_ids,
            unavailable_columns=unavailable_columns,
        )
        return EXIT_OK
    except Stage2RunError as exc:
        print(f"run_stage2: technical failure, run aborted: {exc}", file=sys.stderr)
        return EXIT_TECHNICAL_FAILURE


def _print_summary(
    out, *, run_timestamp, prefilter_ts, stage1_run_timestamp, as_of, csv_path, cache_dir, config_hash, rows, base_summary,
    assessments, price_index, results, ranked, problems, unsupported_rows, missing_ticker_ids, unavailable_columns,
):
    statuses = Counter(r.status for r in results.values())
    levels = Counter(r.peer_level or "none" for r in results.values())
    # per company, not per entry: both metrics can carry the same reason (e.g. no_periods twice)
    reasons = Counter(reason for r in results.values() for reason in set(r.invalid_reasons))
    print(
        f"run_timestamp={run_timestamp}  stage1_run_timestamp={stage1_run_timestamp}  "
        f"prefilter_run_timestamp={prefilter_ts}  as_of={as_of}  stage2_config_hash={config_hash[:12]}",
        file=out,
    )
    print(f"stage2_survivors_csv={csv_path}  fundamentals_cache_dir={cache_dir}", file=out)
    print(
        f"prefilter_rows={len(rows)}  profile_unsupported_rows={unsupported_rows}  peer_base={base_summary.size}  "
        f"survivors_evaluated={len(results)}  ranked={len(ranked)}",
        file=out,
    )
    print("status: " + "  ".join(f"{s}={n}" for s, n in sorted(statuses.items())), file=out)
    print("peer level: " + "  ".join(f"{lv}={n}" for lv, n in sorted(levels.items())), file=out)
    if reasons:
        print(
            f"invalid reasons (Firmen, von {len(results)} Überlebenden): "
            + "  ".join(f"{r}={n}" for r, n in sorted(reasons.items())),
            file=out,
        )
    if problems:
        print(
            f"fundamentals unusable for {len(problems)} company(ies) (not peers): "
            + "  ".join(f"{r}={n}" for r, n in sorted(Counter(problems.values()).items())),
            file=out,
        )
    for column in ("capex", "dividends", "other_investing"):
        tickers = unavailable_columns.get(column)
        if tickers:
            print(f"{column}_unavailable={len(tickers)}  tickers: {sorted(tickers)}", file=out)
    _print_plausibility(out, base_summary, assessments, price_index, results)
    if missing_ticker_ids:
        print(
            f"ACHTUNG: {len(missing_ticker_ids)} Zeile(n) ohne Ticker werden nicht bewertet - simfin_ids: {missing_ticker_ids}",
            file=out,
        )


def _share_counts_line(label, verdicts, unit=""):
    counts = Counter(verdicts)
    checked = counts[SHARE_OK] + counts[SHARE_AMBIGUOUS] + counts[SHARE_SUSPECT]
    return (
        f"{label}: von {len(verdicts)} {unit}geprueft={checked} in_ordnung={counts[SHARE_OK]} "
        f"mehrdeutig={counts[SHARE_AMBIGUOUS]} verdaechtig={counts[SHARE_SUSPECT]} nicht_pruefbar={counts[SHARE_UNCHECKED]}"
    )


def _print_plausibility(out, base_summary, assessments, price_index, results):
    """Numbers of the two plausibility checks (docs/decisions.md "Stufe 2 - Datenplausibilitaet",
    Abschnitt 3), each with its reference size."""
    if price_index is None:
        print("preis_cache: nicht angegeben - Aktienzahl-Pruefung fuer alle Firmen ungeprueft (share_count_unchecked)", file=out)
    else:
        print(
            f"preis_cache: dateien={price_index.files_read} ungueltig={len(price_index.files_invalid)} "
            f"firmen_ungueltig={len(price_index.companies_invalid)} firmen_im_cache={len(price_index.entries)} "
            f"doppelt={price_index.duplicate_ids} nicht_usd={price_index.non_usd}",
            file=out,
        )
        if price_index.files_invalid:
            print(f"ungueltige_dateien: {price_index.files_invalid}", file=out)
    print(
        _share_counts_line("aktienzahl_pruefung_vorfilter", [a.verdict for a in assessments.values()], "Firmen ")
        + "  (Firmen im unterstuetzten Profil mit nutzbaren Basisdaten)",
        file=out,
    )
    survivor_verdicts = {t: assessments[t].verdict if t in assessments else SHARE_UNCHECKED for t in results}
    print(_share_counts_line("aktienzahl_pruefung_ueberlebende", list(survivor_verdicts.values())), file=out)
    suspects = sorted(t for t, verdict in survivor_verdicts.items() if verdict == SHARE_SUSPECT)
    if suspects:
        print(f"verdaechtige Ueberlebende: {suspects}", file=out)
    print(
        f"peer_basis_ohne_verdaechtige: ausgeschieden={len(base_summary.excluded_share_count_suspect)} wegen share_count_suspect "
        f"(Peer-Basis jetzt {base_summary.size})",
        file=out,
    )
    triggered = sum(1 for r in results.values() if r.other_investing_triggered is True)
    flagged = sum(1 for r in results.values() if FLAG_FCF_YIELD_UNVERIFIED in r.flags)
    unchecked = sum(1 for r in results.values() if r.other_investing_triggered is None)
    print(
        f"investitionen_gegenrechnung: von {len(results)} Ueberlebenden ausgeloest={triggered} "
        f"flag_fcf_yield_unverified={flagged} ungeprueft={unchecked}",
        file=out,
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description="Stage 2 (relative valuation) over the fundamentals cache, 0 SimFin calls. "
        "No path has a default: name every database and directory explicitly."
    )
    parser.add_argument("--db-path", type=Path, required=True, help="run archive to READ from (read-only): universe_prefilter and run_results")
    parser.add_argument("--archive-db-path", type=Path, required=True, help="run archive to WRITE stage2_runs/stage2_results to")
    parser.add_argument("--csv-dir", type=Path, required=True)
    parser.add_argument("--fundamentals-cache-dir", type=Path, required=True, help="base directory of the fundamentals cache; the --as-of date is appended")
    parser.add_argument("--stage1-run-timestamp", required=True, help="run_timestamp of the archived Stage-1 run whose survivors are evaluated")
    parser.add_argument("--prefilter-run-timestamp", default=None, help="universe_prefilter run (default: the latest with a passed row)")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None, help="cache-directory date (default: the date of the prefilter run)")
    parser.add_argument("--run-timestamp", default=None, help="archive key of this run (default: now); a repeated value is refused")
    parser.add_argument(
        "--universe-cache-dir", type=Path, default=None,
        help="base directory of the universe run's cache (the --as-of date and 'prices' are appended); optional, "
        "no default: without it the share-count check is unchecked for every company and the report says so",
    )
    parser.add_argument("--thresholds-path", type=Path, default=DEFAULT_THRESHOLDS_PATH)
    parser.add_argument("--stage2-config-path", type=Path, default=DEFAULT_STAGE2_PATH)
    parser.add_argument("--sector-profiles-path", type=Path, default=DEFAULT_SECTOR_PROFILES_PATH)
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
        stage2_config_path=args.stage2_config_path,
        sector_profiles_path=args.sector_profiles_path,
        universe_cache_dir=args.universe_cache_dir,
    )


if __name__ == "__main__":
    sys.exit(main())

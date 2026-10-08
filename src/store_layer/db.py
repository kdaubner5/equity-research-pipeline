"""Store-Layer: run archive (forward-log) + CSV export, per docs/decisions.md.

Issue #6 scope is the run archive only - raw-data caching is explicitly
out of scope here (deferred to roadmap Phase 1.1). Survivors (status
"passed" or "passed_with_flags") are archived in run_results;
gates_layer.rank_companies() is the single source of truth for who counts
as a survivor and their rank position, so this module never re-derives
that filtering itself. Eliminated companies (status "failed" or
"not_evaluable") are archived separately in eliminated_results - the full
funnel scope from masterplan v1.1 (docs/decisions.md, "Store-Layer -
voller Trichter-Scope"). No price-at-run column yet (no price client
exists - Issue #28) and no Stage-3/4 columns (verdict/buy/position size -
those stages aren't built yet).

A "failed" eliminated company is not necessarily complete: since docs/decisions.md
"Phase 2.0d" (Issue #35) a valid hard fail in ROIC, Net Debt/EBITDA or Cash
Conversion makes a company "failed" even if another metric is invalid, so a
"failed" row can carry invalid_reasons and None raw values, just like a
"not_evaluable" one.

A company whose business-model profile is excluded (docs/decisions.md "Phase 2.0e")
has the fifth status "unsupported_business_model": it is archived in
eliminated_results like the other eliminated statuses, with empty
invalid_reasons and no raw values. Both tables also carry the company's
profile and borderline_sector (nullable: NULL = not recorded / not checked,
e.g. rows from before the migration or from the pilot runner); the survivors
CSV carries borderline_sector as its last column (empty string = no).

Both tables (and the survivors CSV) also carry the company's
data_quality_flags, read from GateResult.data_quality_flags - the store never
sees periods, so whoever builds the GateResults passes them in
(gates_layer.stage1_gates.collect_data_quality_flags). docs/decisions.md,
"net_debt-Zuverlaessigkeit - Stock-Flow-Abgleich", Entscheidung 5.
"""

import csv
import os
import sqlite3
from pathlib import Path
from typing import Dict

from gates_layer.stage1_gates import FAILED, GateResult, NOT_EVALUABLE, UNSUPPORTED_BUSINESS_MODEL, rank_companies
from gates_layer.universe_prefilter import EXCLUDED, PASSED, PREFILTER_REASONS
from store_layer.rule_state import write_rule_state

# Anchored to the repo root (src/store_layer/db.py -> repo root is two
# parents up), not the process's current working directory - a cron
# invocation's cwd is not guaranteed to be the repo root, and resolving
# against cwd would silently split the archive across different physical
# files depending on how a run was triggered.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = _REPO_ROOT / "data" / "run_archive.db"
DEFAULT_CSV_DIR = _REPO_ROOT / "data" / "runs"
DEFAULT_UNIVERSE_DIR = _REPO_ROOT / "data" / "universe"

_CSV_FIELDNAMES = [
    "rank_position",
    "ticker",
    "status",
    "flags",
    "roic_3yr",
    "net_debt_ebitda",
    "cash_conversion_3yr",
    "data_quality_flags",
    "borderline_sector",  # last, so the earlier columns keep their positions (docs/decisions.md "Phase 2.0e")
]


def _require_run_timestamp_str(run_timestamp):
    """Guard against e.g. a raw datetime being passed instead of an ISO string.

    Without this, a datetime's .replace(':', '') in export_survivors_csv
    would silently resolve to datetime.replace() (a same-named but
    unrelated method) instead of str.replace(), raising a confusing
    TypeError far from the actual mistake.
    """
    if not isinstance(run_timestamp, str):
        raise TypeError(
            f"run_timestamp must be an ISO-format string (e.g. '2026-09-15T14:30:00'), "
            f"got {type(run_timestamp).__name__}"
        )


def _ensure_config_hash_column(connection, table):
    """Add config_hash to the runs table if it predates the column.

    Deliberately no NOT NULL DEFAULT (unlike _ensure_data_quality_flags_column
    below): NULL is the intended, meaningful value for a pre-migration run
    ("its config_hash was never recorded"), not just an ALTER TABLE artifact
    like data_quality_flags's '' sentinel.
    """
    existing_columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if "config_hash" not in existing_columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN config_hash TEXT")


def _ensure_nullable_column(connection, table, column, sql_type):
    """Add a nullable column to a table created before it existed (docs/decisions.md "Regelstand und Kurs je Lauf").

    No default: existing rows read back NULL, meaning "not recorded then" (like config_hash in runs). A second call
    is a no-op (PRAGMA table_info check). table/column/sql_type are always this module's own constants.
    """
    existing_columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing_columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def _ensure_data_quality_flags_column(connection, table):
    """Add data_quality_flags to a table created before the column existed.

    New tables already have it via CREATE TABLE; an older archive's tables
    (created without it) get it via ALTER TABLE, checked through PRAGMA
    table_info so a second call is a no-op. Existing rows read back as ''
    ("not recorded at the time" is indistinguishable from "no flags" for
    them - accepted, the forward-log is complete from here on).
    `table` is always one of this module's own table-name constants.
    """
    existing_columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if "data_quality_flags" not in existing_columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN data_quality_flags TEXT NOT NULL DEFAULT ''")


def _ensure_profile_columns(connection, table):
    """Add profile TEXT and borderline_sector INTEGER (both nullable) to a table created before them.

    ALTER TABLE ... ADD COLUMN with no default: existing rows are untouched and
    read back NULL, meaning "not recorded then" (docs/decisions.md "Phase 2.0e",
    Entscheidung 5, Option 1 - like config_hash in runs). A second call is a
    no-op (PRAGMA table_info check). `table` is always one of this module's own
    table-name constants.
    """
    existing_columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if "profile" not in existing_columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN profile TEXT")
    if "borderline_sector" not in existing_columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN borderline_sector INTEGER")


def init_db(db_path=DEFAULT_DB_PATH):
    """Create the run_results, eliminated_results, runs and universe_prefilter schema if they don't already exist. Idempotent.

    Also upgrades an existing archive whose run_results/eliminated_results
    tables predate the data_quality_flags column or the profile /
    borderline_sector columns (existing rows unchanged, new columns NULL).
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS run_results (
                run_timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                roic_3yr REAL NOT NULL,
                net_debt_ebitda REAL NOT NULL,
                cash_conversion_3yr REAL NOT NULL,
                status TEXT NOT NULL,
                flags TEXT NOT NULL,
                rank_position INTEGER NOT NULL,
                data_quality_flags TEXT NOT NULL DEFAULT '',
                profile TEXT,
                borderline_sector INTEGER,
                PRIMARY KEY (run_timestamp, ticker)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS eliminated_results (
                run_timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                status TEXT NOT NULL,
                roic REAL,
                net_debt_ebitda REAL,
                cash_conversion REAL,
                net_income_margin REAL,
                invalid_reasons TEXT NOT NULL,
                data_quality_flags TEXT NOT NULL DEFAULT '',
                profile TEXT,
                borderline_sector INTEGER,
                PRIMARY KEY (run_timestamp, ticker)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_timestamp TEXT PRIMARY KEY,
                companies_evaluated INTEGER NOT NULL,
                survivors_count INTEGER NOT NULL,
                config_hash TEXT,
                prefilter_run_timestamp TEXT
            )
            """
        )
        # Universe pre-filter archive (docs/decisions.md, "Phase 2.0a"): the
        # whole pre-filtered universe incl. excluded companies, with the
        # computed values kept for them too. A table added after the others
        # exists, so IF NOT EXISTS is its migration - an older archive gets
        # it on the next init_db(). ticker/sector_code are nullable because
        # SimFin lists entries without either; simfin_id is the stable key.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS universe_prefilter (
                run_timestamp TEXT NOT NULL,
                simfin_id INTEGER NOT NULL,
                ticker TEXT,
                sector_code INTEGER,
                market_cap_usd REAL,
                median_dollar_volume_usd REAL,
                median_window_rows INTEGER,
                price_date TEXT,
                status TEXT NOT NULL,
                reason TEXT,
                config_hash TEXT NOT NULL,
                close_price_usd REAL,
                shares_outstanding REAL,
                PRIMARY KEY (run_timestamp, simfin_id)
            )
            """
        )
        _ensure_data_quality_flags_column(connection, "run_results")
        _ensure_data_quality_flags_column(connection, "eliminated_results")
        _ensure_profile_columns(connection, "run_results")
        _ensure_profile_columns(connection, "eliminated_results")
        _ensure_config_hash_column(connection, "runs")
        _ensure_nullable_column(connection, "runs", "prefilter_run_timestamp", "TEXT")
        _ensure_nullable_column(connection, "universe_prefilter", "close_price_usd", "REAL")
        _ensure_nullable_column(connection, "universe_prefilter", "shares_outstanding", "REAL")
        connection.commit()
    finally:
        connection.close()


def _borderline_db_value(borderline_sector):
    """borderline_sector as stored: None (not checked) stays NULL, otherwise 0/1."""
    return None if borderline_sector is None else int(bool(borderline_sector))


def _survivor_rows(gate_results: Dict[str, GateResult]):
    """Ranked survivor rows (status passed/passed_with_flags).

    rank_position is the 1-based position in rank_companies()'s overall
    ordered list (flag-count groups, ticker-sorted within a group) - not
    reset to 1 at the start of each flag-count group.
    """
    ranked = rank_companies(gate_results)
    return [
        {
            "ticker": ticker,
            "roic_3yr": result.roic,
            "net_debt_ebitda": result.net_debt_ebitda,
            "cash_conversion_3yr": result.cash_conversion,
            "status": result.status,
            "flags": ",".join(result.flags),
            "rank_position": position,
            "data_quality_flags": ",".join(result.data_quality_flags),
            "profile": result.profile,
            "borderline_sector": _borderline_db_value(result.borderline_sector),
        }
        for position, (ticker, result) in enumerate(ranked, start=1)
    ]


def _eliminated_rows(gate_results: Dict[str, GateResult]):
    """Eliminated rows (status failed/not_evaluable/unsupported_business_model), unranked.

    An unsupported_business_model company has all four raw values None and empty
    invalid_reasons (no metric was computed, and it is not a data gap).

    The four raw values stay None wherever the underlying MetricResult was
    invalid, and invalid_reasons lists every such reason. That holds for
    not_evaluable and, since docs/decisions.md "Phase 2.0d" (Issue #35),
    also for a failed company whose valid ROIC / Net Debt/EBITDA / Cash
    Conversion is in its hard-fail zone next to an invalid metric (see
    gates_layer.stage1_gates.evaluate_stage1_gates). A failed company
    without any invalid metric still has all four values and an empty
    invalid_reasons.
    """
    return [
        {
            "ticker": ticker,
            "status": result.status,
            "roic": result.roic,
            "net_debt_ebitda": result.net_debt_ebitda,
            "cash_conversion": result.cash_conversion,
            "net_income_margin": result.net_income_margin,
            "invalid_reasons": ",".join(result.invalid_reasons),
            "data_quality_flags": ",".join(result.data_quality_flags),
            "profile": result.profile,
            "borderline_sector": _borderline_db_value(result.borderline_sector),
        }
        for ticker, result in gate_results.items()
        if result.status in (FAILED, NOT_EVALUABLE, UNSUPPORTED_BUSINESS_MODEL)
    ]


def save_run_results(
    run_timestamp, gate_results, *, config_hash, rule_state, prefilter_run_timestamp=None, db_path=DEFAULT_DB_PATH
):
    """Persist one run: exactly one `runs` row, its survivors in `run_results`,
    and its eliminated companies in `eliminated_results`.

    rule_state (store_layer.rule_state.RuleState, docs/decisions.md "Regelstand und Kurs je Lauf", Punkt 6) is
    written in the same transaction as step 'stage1'; required, no default, None for a caller with no real rule
    state (e.g. a test of archiving mechanics) - then no rule-state rows are written. prefilter_run_timestamp is
    the universe_prefilter run the survivors came from (NULL = not recorded).

    config_hash (docs/decisions.md, "Schwellen nach config/thresholds.yaml")
    is the SHA-256 of the thresholds file that produced this run's
    gate_results, or None for a caller with no real config to report (e.g. a
    test exercising archiving mechanics in isolation). Required, no default -
    a silent default here would undercut the same "no defaults" principle the
    thresholds themselves are loaded under. Pre-migration runs read back NULL
    (see _ensure_config_hash_column).

    The `runs` row is written on every call, regardless of survivors_count -
    a 0-survivor run is an explicitly expected outcome (masterplan.md §4,
    "Kein Zwangskauf": below 8 qualifiers, cash is held rather than lowering
    the bar), not an edge case, and needs proof it happened at all,
    independent of whether anyone survived it. The same applies symmetrically
    to a 0-eliminated run.

    All three inserts share one connection/transaction (single commit): a
    failed insert into any one of the three (e.g. sqlite3.IntegrityError on
    a re-saved run_timestamp - a plain INSERT, not INSERT OR REPLACE, since
    the forward-log is a historical record that must never be silently
    overwritten) must not leave the other two with orphaned rows for this
    run_timestamp.
    """
    _require_run_timestamp_str(run_timestamp)
    db_path = Path(db_path)
    survivor_rows = _survivor_rows(gate_results)
    eliminated_rows = _eliminated_rows(gate_results)

    # Guard against a silent partition gap (Review-Fund): _survivor_rows()
    # and _eliminated_rows() each independently check a company's status
    # against their own fixed set of status strings (PASSED/PASSED_WITH_FLAGS
    # vs. FAILED/NOT_EVALUABLE/UNSUPPORTED_BUSINESS_MODEL). Those two sets only
    # exhaustively partition every company because exactly five status strings
    # exist today
    # (gates_layer.stage1_gates deliberately keeps status a plain string,
    # not an enum - see its Entscheidung 5). An unrecognized status (a
    # future fifth status added to one filter but not the other, or a
    # malformed GateResult) would otherwise vanish from both tables with no
    # exception raised - exactly the silent data loss CLAUDE.md's data-
    # integrity convention forbids.
    accounted_for = {row["ticker"] for row in survivor_rows} | {row["ticker"] for row in eliminated_rows}
    if accounted_for != set(gate_results):
        raise ValueError(
            f"gate_results contains companies with an unrecognized status that "
            f"landed in neither run_results nor eliminated_results: "
            f"{sorted(set(gate_results) - accounted_for)}"
        )

    init_db(db_path)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO runs (run_timestamp, companies_evaluated, survivors_count, config_hash, prefilter_run_timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_timestamp, len(gate_results), len(survivor_rows), config_hash, prefilter_run_timestamp),
        )
        if rule_state is not None:
            write_rule_state(connection, "stage1", run_timestamp, rule_state)
        connection.executemany(
            """
            INSERT INTO run_results (
                run_timestamp, ticker, roic_3yr, net_debt_ebitda,
                cash_conversion_3yr, status, flags, rank_position, data_quality_flags,
                profile, borderline_sector
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_timestamp,
                    row["ticker"],
                    row["roic_3yr"],
                    row["net_debt_ebitda"],
                    row["cash_conversion_3yr"],
                    row["status"],
                    row["flags"],
                    row["rank_position"],
                    row["data_quality_flags"],
                    row["profile"],
                    row["borderline_sector"],
                )
                for row in survivor_rows
            ],
        )
        connection.executemany(
            """
            INSERT INTO eliminated_results (
                run_timestamp, ticker, status, roic, net_debt_ebitda,
                cash_conversion, net_income_margin, invalid_reasons, data_quality_flags,
                profile, borderline_sector
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_timestamp,
                    row["ticker"],
                    row["status"],
                    row["roic"],
                    row["net_debt_ebitda"],
                    row["cash_conversion"],
                    row["net_income_margin"],
                    row["invalid_reasons"],
                    row["data_quality_flags"],
                    row["profile"],
                    row["borderline_sector"],
                )
                for row in eliminated_rows
            ],
        )
        connection.commit()
    finally:
        connection.close()


def export_survivors_csv(run_timestamp, gate_results, output_dir=DEFAULT_CSV_DIR):
    """Write one run's survivors to a timestamped CSV snapshot. Returns its path.

    Filename is survivors_<run_timestamp>.csv with colons stripped from the
    timestamp (invalid in Windows filenames), e.g.
    "2026-09-15T14:30:00" -> "survivors_2026-09-15T143000.csv".

    Always writes a file, even with 0 survivors (header row only, same
    reasoning as save_run_results()'s `runs` row) - a missing file must
    never be mistaken for "no run happened".
    """
    _require_run_timestamp_str(run_timestamp)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / f"survivors_{run_timestamp.replace(':', '')}.csv"
    rows = _survivor_rows(gate_results)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            # borderline_sector: empty string = no (or not checked), 1 = yes (docs/decisions.md "Phase 2.0e")
            writer.writerow(
                {name: ("1" if row[name] else "") if name == "borderline_sector" else row[name] for name in _CSV_FIELDNAMES}
            )

    return csv_path


def _prefilter_rows(results):
    """Validate a universe pre-filter run and return its INSERT rows.

    Refuses (ValueError) an empty run - it would leave no trace that it
    happened, and an empty universe is a run failure, not an archivable
    outcome -, a company listed twice, an unrecognized status, a reason on a
    passed company, and a reason outside the documented vocabulary. Nothing
    is silently dropped or coerced.
    """
    results = list(results)
    if not results:
        raise ValueError("no pre-filter results to archive")
    seen, rows = set(), []
    for result in results:
        if result.simfin_id in seen:
            raise ValueError(f"simfin_id {result.simfin_id} appears twice in the pre-filter results")
        seen.add(result.simfin_id)
        if result.status == PASSED:
            if result.reason is not None:
                raise ValueError(f"passed company {result.simfin_id} carries reason {result.reason!r}")
        elif result.status == EXCLUDED:
            if result.reason not in PREFILTER_REASONS:
                raise ValueError(f"company {result.simfin_id} has unrecognized reason {result.reason!r}")
        else:
            raise ValueError(f"company {result.simfin_id} has unrecognized status {result.status!r}")
        rows.append(
            (
                result.simfin_id,
                result.ticker,
                result.sector_code,
                result.market_cap_usd,
                result.median_dollar_volume_usd,
                result.median_window_rows,
                result.price_date.isoformat() if result.price_date is not None else None,
                result.status,
                result.reason,
                result.close_price_usd,
                result.shares_outstanding,
            )
        )
    return rows


def save_universe_prefilter(run_timestamp, results, *, config_hash, rule_state, db_path=DEFAULT_DB_PATH):
    """Persist one universe pre-filter run: one universe_prefilter row per
    company - passed and excluded alike, with market cap / median dollar
    volume / price date kept for the excluded ones too.

    config_hash is the SHA-256 of the thresholds file that produced the
    results (docs/decisions.md, "Schwellen nach config/thresholds.yaml");
    required, no default, and - unlike runs.config_hash, whose NULL means
    "run predates the column" - never NULL here, since this table is new.
    Plain INSERT in one transaction, never REPLACE: the archive is a
    historical record, so re-saving a (run_timestamp, simfin_id) fails
    (sqlite3.IntegrityError) and leaves the run's other rows unwritten.

    Each row also keeps the close price and the share count of the last priced row (close_price_usd /
    shares_outstanding, NULL where the data has none), and rule_state (required, None = none to report) is
    written as step 'prefilter' in the same transaction (docs/decisions.md "Regelstand und Kurs je Lauf").
    """
    _require_run_timestamp_str(run_timestamp)
    if not isinstance(config_hash, str) or not config_hash:
        raise ValueError("config_hash must be a non-empty string")
    rows = _prefilter_rows(results)

    init_db(db_path)
    connection = sqlite3.connect(Path(db_path))
    try:
        connection.executemany(
            """
            INSERT INTO universe_prefilter (
                run_timestamp, simfin_id, ticker, sector_code, market_cap_usd,
                median_dollar_volume_usd, median_window_rows, price_date,
                status, reason, config_hash, close_price_usd, shares_outstanding
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(run_timestamp, *row[:9], config_hash, *row[9:]) for row in rows],
        )
        if rule_state is not None:
            write_rule_state(connection, "prefilter", run_timestamp, rule_state)
        connection.commit()
    finally:
        connection.close()


def export_universe_survivors(run_timestamp, results, output_dir=DEFAULT_UNIVERSE_DIR):
    """Write the passed companies' tickers, one per line, to a timestamped
    file and return its path - the same format as config/universe.txt, so it
    can be used as a universe file. Never touches config/universe.txt.

    Filename survivors_<run_timestamp>.txt with colons stripped (like the
    survivors CSV). Always written, even with 0 survivors (empty file), so a
    missing file never reads as "no run happened". Refuses (ValueError) a
    passed company without a ticker or a ticker appearing twice (compared
    case-insensitively) - the file would be ambiguous. Written atomically.
    """
    _require_run_timestamp_str(run_timestamp)
    tickers = []
    for result in results:
        if result.status != PASSED:
            continue
        if not result.ticker:
            raise ValueError(f"passed company {result.simfin_id} has no ticker - cannot be listed")
        tickers.append(result.ticker)
    tickers.sort(key=str.upper)
    upper = [ticker.upper() for ticker in tickers]
    if len(set(upper)) != len(upper):
        raise ValueError("a ticker appears twice among the passed companies")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"survivors_{run_timestamp.replace(':', '')}.txt"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(f"{ticker}\n" for ticker in tickers), encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return path

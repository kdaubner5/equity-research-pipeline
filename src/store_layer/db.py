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
"""

import csv
import sqlite3
from pathlib import Path
from typing import Dict

from gates_layer.stage1_gates import FAILED, GateResult, NOT_EVALUABLE, rank_companies

# Anchored to the repo root (src/store_layer/db.py -> repo root is two
# parents up), not the process's current working directory - a cron
# invocation's cwd is not guaranteed to be the repo root, and resolving
# against cwd would silently split the archive across different physical
# files depending on how a run was triggered.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = _REPO_ROOT / "data" / "run_archive.db"
DEFAULT_CSV_DIR = _REPO_ROOT / "data" / "runs"

_CSV_FIELDNAMES = [
    "rank_position",
    "ticker",
    "status",
    "flags",
    "roic_3yr",
    "net_debt_ebitda",
    "cash_conversion_3yr",
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


def init_db(db_path=DEFAULT_DB_PATH):
    """Create the run_results, eliminated_results and runs schema if they don't already exist. Idempotent."""
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
                PRIMARY KEY (run_timestamp, ticker)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_timestamp TEXT PRIMARY KEY,
                companies_evaluated INTEGER NOT NULL,
                survivors_count INTEGER NOT NULL
            )
            """
        )
        connection.commit()
    finally:
        connection.close()


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
        }
        for position, (ticker, result) in enumerate(ranked, start=1)
    ]


def _eliminated_rows(gate_results: Dict[str, GateResult]):
    """Eliminated rows (status failed/not_evaluable), unranked.

    The four raw values stay None wherever the underlying MetricResult was
    invalid (only possible for not_evaluable - a failed company always has
    all four populated, since classification only runs once every metric
    is valid, see gates_layer.stage1_gates.evaluate_stage1_gates).
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
        }
        for ticker, result in gate_results.items()
        if result.status in (FAILED, NOT_EVALUABLE)
    ]


def save_run_results(run_timestamp, gate_results, db_path=DEFAULT_DB_PATH):
    """Persist one run: exactly one `runs` row, its survivors in `run_results`,
    and its eliminated companies in `eliminated_results`.

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
    init_db(db_path)
    survivor_rows = _survivor_rows(gate_results)
    eliminated_rows = _eliminated_rows(gate_results)

    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO runs (run_timestamp, companies_evaluated, survivors_count) VALUES (?, ?, ?)",
            (run_timestamp, len(gate_results), len(survivor_rows)),
        )
        connection.executemany(
            """
            INSERT INTO run_results (
                run_timestamp, ticker, roic_3yr, net_debt_ebitda,
                cash_conversion_3yr, status, flags, rank_position
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                )
                for row in survivor_rows
            ],
        )
        connection.executemany(
            """
            INSERT INTO eliminated_results (
                run_timestamp, ticker, status, roic, net_debt_ebitda,
                cash_conversion, net_income_margin, invalid_reasons
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            writer.writerow({name: row[name] for name in _CSV_FIELDNAMES})

    return csv_path

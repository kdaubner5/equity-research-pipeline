"""Trend-Guard archive: two own tables and a CSV (docs/decisions.md "Trend-Guard (Masterplan Stufe 1.1,
Hebel 3 aus Issue #52)", Abschnitt 3).

trend_runs holds one row per run (written even with 0 results, so a run leaves proof it happened);
trend_results one row per evaluated Stage-1 survivor. Both are created with CREATE TABLE IF NOT EXISTS,
which is their whole migration: no existing table, no Stage-1/Stage-2 code and no existing row is
touched. A missing value is stored as NULL, never as 0. There is deliberately no default database path
here - the caller (run_trend_guard.py) must name the archive explicitly.
"""

import csv
import sqlite3
from pathlib import Path

TREND_CSV_FIELDNAMES = [
    "ticker",
    "status",
    "marks",
    "mark_count",
    "revenue_ratio",
    "ebitda_margin_ratio",
    "roic_last_year",
    "revenue_invalid_reason",
    "margin_invalid_reason",
    "roic_invalid_reason",
]


def _require_str(name, value):
    if not isinstance(value, str):
        raise TypeError(f"{name} must be an ISO string, got {type(value).__name__}")


def init_trend_tables(db_path):
    """Create trend_runs and trend_results if they do not exist (idempotent)."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trend_runs (
                run_timestamp TEXT PRIMARY KEY,
                stage1_run_timestamp TEXT NOT NULL,
                prefilter_run_timestamp TEXT NOT NULL,
                as_of TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                companies_evaluated INTEGER NOT NULL,
                marked_count INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trend_results (
                run_timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                simfin_id INTEGER,
                status TEXT NOT NULL,
                marks TEXT NOT NULL,
                mark_count INTEGER NOT NULL,
                revenue_ratio REAL,
                ebitda_margin_ratio REAL,
                roic_last_year REAL,
                revenue_invalid_reason TEXT,
                margin_invalid_reason TEXT,
                roic_invalid_reason TEXT,
                PRIMARY KEY (run_timestamp, ticker)
            )
            """
        )
        connection.commit()
    finally:
        connection.close()


def _result_rows(run_timestamp, results, simfin_ids):
    rows = []
    for ticker, r in sorted(results.items()):
        rows.append(
            (
                run_timestamp, ticker, simfin_ids.get(ticker), r.status, ",".join(r.marks), r.mark_count,
                r.revenue_ratio, r.ebitda_margin_ratio, r.roic_last_year,
                r.revenue_invalid_reason, r.margin_invalid_reason, r.roic_invalid_reason,
            )
        )
    return rows


def save_trend_results(
    run_timestamp, *, results, stage1_run_timestamp, prefilter_run_timestamp, as_of, config_hash, simfin_ids, db_path
):
    """Persist one Trend-Guard run in a single transaction: the trend_runs row and one trend_results row per
    company in results (ticker -> TrendResult). simfin_ids maps tickers to SimFin ids (a ticker without one
    is stored with NULL, never a made-up id). A repeated run_timestamp raises sqlite3.IntegrityError and
    writes nothing. Rows are validated before the database is opened."""
    _require_str("run_timestamp", run_timestamp)
    rows = _result_rows(run_timestamp, results, simfin_ids)
    init_trend_tables(db_path)
    connection = sqlite3.connect(db_path)
    try:
        with connection:  # one transaction: commit on success, rollback on any exception
            connection.execute(
                "INSERT INTO trend_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_timestamp, stage1_run_timestamp, prefilter_run_timestamp, str(as_of), config_hash,
                    len(results), sum(1 for r in results.values() if r.mark_count),
                ),
            )
            connection.executemany("INSERT INTO trend_results VALUES (" + ",".join("?" * 12) + ")", rows)
    finally:
        connection.close()


def export_trend_csv(run_timestamp, results, *, output_dir):
    """Write every evaluated company (sorted by ticker) to trend_guard_<run_timestamp>.csv (colons removed);
    an existing file is never overwritten (FileExistsError). With no company the file holds the header only."""
    _require_str("run_timestamp", run_timestamp)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"trend_guard_{run_timestamp.replace(':', '')}.csv"
    with open(path, "x", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TREND_CSV_FIELDNAMES)
        writer.writeheader()
        for ticker, r in sorted(results.items()):
            writer.writerow(
                {
                    "ticker": ticker, "status": r.status, "marks": ",".join(r.marks), "mark_count": r.mark_count,
                    "revenue_ratio": _csv(r.revenue_ratio), "ebitda_margin_ratio": _csv(r.ebitda_margin_ratio),
                    "roic_last_year": _csv(r.roic_last_year), "revenue_invalid_reason": _csv(r.revenue_invalid_reason),
                    "margin_invalid_reason": _csv(r.margin_invalid_reason), "roic_invalid_reason": _csv(r.roic_invalid_reason),
                }
            )
    return path


def _csv(value):
    return "" if value is None else value

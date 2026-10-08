"""Stage-2 archive: two own tables and a CSV (docs/decisions.md "Stufe 2 - Beantwortung
Q1-Q9 und Runner", Entscheidung R2).

stage2_runs holds one row per run (written even with 0 results, so a run leaves proof it
happened); stage2_results one row per evaluated Stage-1 survivor. Both are created with
CREATE TABLE IF NOT EXISTS, which is their whole migration: no existing table, no
Stage-1 code and no existing row is touched. A missing value is stored as NULL, never as 0.

There is deliberately no default database path here - the caller (run_stage2.py) must name
the archive explicitly.
"""

import csv
import sqlite3
from pathlib import Path

from store_layer.rule_state import write_rule_state

STAGE2_CSV_FIELDNAMES = [
    "rank_position",
    "ticker",
    "status",
    "flags",
    "ev_ebitda",
    "ev_ebitda_percentile",
    "peer_level",
    "peer_group",
    "peer_rank",
    "peer_n",
    "fcf_yield",
    "ev_ebitda_normalized",
    "info_flags",
    # appended by docs/decisions.md "Stufe 2 - Datenplausibilitaet", so the earlier columns keep their positions
    "share_count_ratio",
    "fcf_yield_incl_other_investing",
]


def _require_str(name, value):
    if not isinstance(value, str):
        raise TypeError(f"{name} must be an ISO string, got {type(value).__name__}")


def init_stage2_tables(db_path):
    """Create stage2_runs and stage2_results if they do not exist (idempotent)."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stage2_runs (
                run_timestamp TEXT PRIMARY KEY,
                stage1_run_timestamp TEXT NOT NULL,
                prefilter_run_timestamp TEXT NOT NULL,
                as_of TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                companies_evaluated INTEGER NOT NULL,
                ranked_count INTEGER NOT NULL,
                peer_base_size INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stage2_results (
                run_timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                simfin_id INTEGER,
                status TEXT NOT NULL,
                flags TEXT NOT NULL,
                invalid_reasons TEXT NOT NULL,
                info_flags TEXT NOT NULL,
                rank_position INTEGER,
                ev_ebitda REAL,
                ev_ebitda_percentile REAL,
                peer_level TEXT,
                peer_group INTEGER,
                peer_rank INTEGER,
                peer_n INTEGER,
                peer_n_fine INTEGER,
                peer_n_coarse INTEGER,
                fcf_yield REAL,
                ev_ebitda_normalized REAL,
                ev_ebitda_normalized_invalid_reason TEXT,
                share_count_ratio REAL,
                fcf_yield_incl_other_investing REAL,
                other_investing_triggered INTEGER,
                PRIMARY KEY (run_timestamp, ticker)
            )
            """
        )
        connection.commit()
    finally:
        connection.close()


def _result_rows(run_timestamp, results, ranked, simfin_ids):
    positions = {}
    for position, (ticker, _) in enumerate(ranked, start=1):
        if ticker not in results:
            raise ValueError(f"ranked company {ticker!r} has no entry in results")
        positions[ticker] = position
    rows = []
    for ticker, r in sorted(results.items()):
        rows.append(
            (
                run_timestamp,
                ticker,
                simfin_ids.get(ticker),
                r.status,
                ",".join(r.flags),
                ",".join(r.invalid_reasons),
                ",".join(r.info_flags),
                positions.get(ticker),
                r.ev_ebitda,
                r.ev_ebitda_percentile,
                r.peer_level,
                r.peer_group,
                r.peer_rank,
                r.peer_n,
                r.peer_n_fine,
                r.peer_n_coarse,
                r.fcf_yield,
                r.ev_ebitda_normalized,
                r.ev_ebitda_normalized_invalid_reason,
                r.share_count_ratio,
                r.fcf_yield_incl_other_investing,
                None if r.other_investing_triggered is None else int(bool(r.other_investing_triggered)),
            )
        )
    return rows


def save_stage2_results(
    run_timestamp,
    *,
    results,
    ranked,
    stage1_run_timestamp,
    prefilter_run_timestamp,
    as_of,
    config_hash,
    peer_base_size,
    simfin_ids,
    rule_state,
    db_path,
):
    """Persist one Stage-2 run in a single transaction: the stage2_runs row and one
    stage2_results row per company in results (ticker -> Stage2Result). rule_state (store_layer.rule_state.RuleState;
    required, None = none to report) is written as step 'stage2' in the same transaction.

    ranked is gates_layer.stage2_gates.rank_stage2()'s output; only those companies get a
    rank_position (1-based, in that order). simfin_ids maps every ticker in results to its
    SimFin id (a ticker without one - e.g. a Stage-1 survivor absent from the pre-filtered
    universe - is stored with NULL, never a made-up id). A repeated run_timestamp raises sqlite3.IntegrityError and writes nothing.
    Rows are validated before the database is opened.
    """
    _require_str("run_timestamp", run_timestamp)
    rows = _result_rows(run_timestamp, results, ranked, simfin_ids)
    init_stage2_tables(db_path)
    connection = sqlite3.connect(db_path)
    try:
        with connection:  # one transaction: commit on success, rollback on any exception
            connection.execute(
                "INSERT INTO stage2_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_timestamp,
                    stage1_run_timestamp,
                    prefilter_run_timestamp,
                    str(as_of),
                    config_hash,
                    len(results),
                    len(ranked),
                    peer_base_size,
                ),
            )
            connection.executemany("INSERT INTO stage2_results VALUES (" + ",".join("?" * 22) + ")", rows)
            if rule_state is not None:
                write_rule_state(connection, "stage2", run_timestamp, rule_state)
    finally:
        connection.close()


def export_stage2_csv(run_timestamp, ranked, *, output_dir):
    """Write the companies that passed Stage 2 (rank_stage2() order) to
    stage2_survivors_<run_timestamp>.csv (colons removed from the timestamp); an existing file is never
    overwritten (FileExistsError). With nobody ranked the file holds the header only."""
    _require_str("run_timestamp", run_timestamp)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"stage2_survivors_{run_timestamp.replace(':', '')}.csv"
    with open(path, "x", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=STAGE2_CSV_FIELDNAMES)
        writer.writeheader()
        for position, (ticker, r) in enumerate(ranked, start=1):
            writer.writerow(
                {
                    "rank_position": position,
                    "ticker": ticker,
                    "status": r.status,
                    "flags": ",".join(r.flags),
                    "ev_ebitda": _csv(r.ev_ebitda),
                    "ev_ebitda_percentile": _csv(r.ev_ebitda_percentile),
                    "peer_level": _csv(r.peer_level),
                    "peer_group": _csv(r.peer_group),
                    "peer_rank": _csv(r.peer_rank),
                    "peer_n": _csv(r.peer_n),
                    "fcf_yield": _csv(r.fcf_yield),
                    "ev_ebitda_normalized": _csv(r.ev_ebitda_normalized),
                    "info_flags": ",".join(r.info_flags),
                    "share_count_ratio": _csv(r.share_count_ratio),
                    "fcf_yield_incl_other_investing": _csv(r.fcf_yield_incl_other_investing),
                }
            )
    return path


def _csv(value):
    return "" if value is None else value

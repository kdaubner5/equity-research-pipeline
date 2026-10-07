"""Archive and CSV columns of the plausibility checks (docs/decisions.md "Stufe 2 -
Datenplausibilitaet: Aktienzahl und Investitionen", Abschnitt 3)."""

import csv
import dataclasses
import sqlite3

from gates_layer.stage1_gates import PASSED_WITH_FLAGS
from store_layer.stage2_store import STAGE2_CSV_FIELDNAMES, export_stage2_csv, init_stage2_tables, save_stage2_results
from test_stage2_store import RUN_TS, result, rows, save


def test_new_columns_exist_after_the_old_ones_in_the_csv():
    assert STAGE2_CSV_FIELDNAMES[-2:] == ["share_count_ratio", "fcf_yield_incl_other_investing"]
    assert STAGE2_CSV_FIELDNAMES[0] == "rank_position" and "info_flags" in STAGE2_CSV_FIELDNAMES


def test_the_new_values_round_trip_and_none_stays_null(tmp_path):
    db = tmp_path / "a.db"
    full = dataclasses.replace(result(PASSED_WITH_FLAGS, flags=("fcf_yield_unverified",)), share_count_ratio=4.2,
                               fcf_yield_incl_other_investing=-0.031, other_investing_triggered=True)
    none = result()  # the defaults: nothing checked
    save(db, {"AAA": full, "BBB": none}, [("AAA", full)])
    got = {r[0]: r[1:] for r in rows(db, "SELECT ticker, share_count_ratio, fcf_yield_incl_other_investing, other_investing_triggered, flags FROM stage2_results")}
    assert got["AAA"] == (4.2, -0.031, 1, "fcf_yield_unverified")
    assert got["BBB"] == (None, None, None, "")


def test_other_investing_triggered_false_is_stored_as_zero_not_null(tmp_path):
    db = tmp_path / "a.db"
    save(db, {"AAA": dataclasses.replace(result(), other_investing_triggered=False)}, [])
    assert rows(db, "SELECT other_investing_triggered FROM stage2_results") == [(0,)]


def test_the_csv_carries_the_new_columns_with_empty_cells_for_none(tmp_path):
    ranked = [("AAA", dataclasses.replace(result(), share_count_ratio=3.5, fcf_yield_incl_other_investing=0.07)), ("BBB", result())]
    path = export_stage2_csv(RUN_TS, ranked, output_dir=tmp_path)
    with open(path, encoding="utf-8", newline="") as f:
        data = list(csv.DictReader(f))
    assert (data[0]["share_count_ratio"], data[0]["fcf_yield_incl_other_investing"]) == ("3.5", "0.07")
    assert (data[1]["share_count_ratio"], data[1]["fcf_yield_incl_other_investing"]) == ("", "")


def test_init_is_still_idempotent_and_adds_no_table(tmp_path):
    db = tmp_path / "a.db"
    init_stage2_tables(db)
    init_stage2_tables(db)
    assert {r[0] for r in rows(db, "SELECT name FROM sqlite_master WHERE type = 'table'")} == {"stage2_runs", "stage2_results"}

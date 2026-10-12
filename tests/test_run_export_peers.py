"""run_export_peers.py: vollstaendige Peer-Gruppen aus dem Zwischenspeicher-Eingaben (nur Rechnung, kein Abruf)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_export_peers as rep
from stage2_helpers import make_config
from test_stage2_gates import company, group

CONFIG = make_config()
FINE = 107002


def inputs_with_group(multiples, sector=110001):
    return group(sector, multiples, prefix="P")


def test_all_firms_of_the_candidates_group_are_listed_with_rank_and_size():
    inputs = inputs_with_group([4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0])
    rows, notes = rep.peer_rows(inputs, CONFIG, 2026, ["P2"], {"P2": "passed", "P0": "passed"})
    assert notes == [] and len(rows) == 8
    by = {r["ticker"]: r for r in rows}
    assert by["P2"]["peer_rank"] == 3 and by["P2"]["peer_n"] == 8 and by["P2"]["peer_level"] == "fine"
    assert by["P2"]["status"] == "passed" and by["P5"]["status"] == "nicht_in_stufe_2"
    assert all(r["candidate"] == "P2" and r["peer_group"] == 110001 for r in rows)
    assert by["P0"]["ev_ebitda"] == pytest.approx(4.0) and by["P0"]["ev_ebitda_percentile"] == 0.0


def test_other_sectors_are_not_part_of_the_group():
    inputs = inputs_with_group([4.0 + i for i in range(8)])
    inputs.update(group(FINE, [5.0, 6.0], prefix="X"))
    rows, _ = rep.peer_rows(inputs, CONFIG, 2026, ["P0"], {})
    assert not any(r["ticker"].startswith("X") for r in rows)


def test_coarse_level_collects_the_whole_coarse_group():
    inputs = {}
    for i, sector in enumerate((110001, 110002, 110003, 110004)):
        inputs.update(group(sector, [4.0 + i, 8.0 + i], prefix=f"S{sector}_"))
    rows, _ = rep.peer_rows(inputs, CONFIG, 2026, ["S110001_0"], {})
    assert rows and rows[0]["peer_level"] == "coarse" and rows[0]["peer_group"] == 110 and len(rows) == 8


def test_a_candidate_without_peers_or_peer_base_entry_gets_a_note_not_silence():
    inputs = inputs_with_group([4.0, 5.0, 6.0])
    inputs["LONE"] = company("LONE", 105001, multiple=5.0)
    inputs["BAD"] = company("BAD", 110001, multiple=5.0, ebitda=-1.0)
    rows, notes = rep.peer_rows(inputs, CONFIG, 2026, ["LONE", "BAD", "NOPE"], {})
    assert rows == []
    assert any("LONE" in n and "absolute Ebene" in n for n in notes)
    assert any("NOPE" in n and "nicht in der Peer-Basis" in n for n in notes)


def test_a_peer_with_unusable_fcf_keeps_its_row_with_the_reason():
    inputs = inputs_with_group([4.0 + i for i in range(8)])
    inputs["P3"] = company("P3", 110001, multiple=7.0, capex_by_year={})
    rows, _ = rep.peer_rows(inputs, CONFIG, 2026, ["P0"], {})
    p3 = next(r for r in rows if r["ticker"] == "P3")
    assert p3["fcf_yield"] is None and p3["invalid_reasons"] != ""


def test_main_refuses_the_real_archive_a_missing_file_and_an_existing_output(tmp_path, capsys):
    from store_layer import db as archive_db
    base = ["x", "2026-10-10T12:27:44", "--fundamentals-cache-dir", str(tmp_path)]
    assert rep.main([str(archive_db.DEFAULT_DB_PATH)] + base[1:] + ["--fundamentals-cache-dir", str(tmp_path), "--out", str(tmp_path / "o.csv")]) == 2
    assert "echte Run-Archiv" in capsys.readouterr().err
    assert rep.main([str(tmp_path / "fehlt.db"), "2026-10-10T12:27:44", "--fundamentals-cache-dir", str(tmp_path), "--out", str(tmp_path / "o.csv")]) == 2
    db = tmp_path / "k.db"
    db.write_bytes(b"")
    out = tmp_path / "o.csv"
    out.write_text("x", encoding="utf-8")
    assert rep.main([str(db), "2026-10-10T12:27:44", "--fundamentals-cache-dir", str(tmp_path), "--out", str(out)]) == 2
    assert out.read_text(encoding="utf-8") == "x"

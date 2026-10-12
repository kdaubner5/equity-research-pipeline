"""run_export_peers.py: vollstaendige Peer-Gruppen aus dem Zwischenspeicher-Eingaben (nur Rechnung, kein Abruf)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_export_peers as rep
from stage2_helpers import make_config
from gates_layer.stage1_gates import PASSED
from gates_layer.stage2_gates import evaluate_stage2
from test_stage2_gates import AS_OF, company, group, s1

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
    common = ["2026-10-10T12:27:44", "--fundamentals-cache-dir", str(tmp_path), "--universe-cache-dir", str(tmp_path)]
    assert rep.main([str(archive_db.DEFAULT_DB_PATH)] + common + ["--out", str(tmp_path / "o.csv")]) == 2
    assert "echte Run-Archiv" in capsys.readouterr().err
    assert rep.main([str(tmp_path / "fehlt.db")] + common + ["--out", str(tmp_path / "o.csv")]) == 2
    db = tmp_path / "k.db"
    db.write_bytes(b"")
    out = tmp_path / "o.csv"
    out.write_text("x", encoding="utf-8")
    assert rep.main([str(db)] + common + ["--out", str(out)]) == 2
    assert out.read_text(encoding="utf-8") == "x"


def test_rank_and_group_size_match_what_stage_2_itself_computes():
    inputs = {}
    inputs.update(group(110001, [4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0], prefix="A"))
    inputs.update(group(110002, [3.0, 5.5, 6.5, 9.5, 10.5, 12.5, 13.5, 14.5], prefix="B"))
    survivors = {"A2": s1(), "A5": s1(), "B1": s1()}
    results = evaluate_stage2(inputs, survivors, as_of=AS_OF, config=CONFIG)
    for ticker in survivors:
        rows, _ = rep.peer_rows(inputs, CONFIG, AS_OF.year, [ticker], {})
        own = next(r for r in rows if r["ticker"] == ticker)
        assert own["peer_rank"] == results[ticker].peer_rank
        assert own["peer_n"] == results[ticker].peer_n == len(rows)
        assert own["peer_group"] == results[ticker].peer_group and own["peer_level"] == results[ticker].peer_level
        assert own["ev_ebitda_percentile"] == pytest.approx(float(results[ticker].ev_ebitda_percentile))


def test_notes_name_the_cause_and_the_firms_lost_to_missing_data():
    inputs = inputs_with_group([4.0 + i for i in range(8)])
    inputs["NEG"] = company("NEG", 110001, multiple=5.0, ebitda=-1.0)
    problems = {"GONE": (110001, "fundamentals_cache_missing"), "ELSE": (120001, "fundamentals_cache_missing")}
    rows, notes = rep.peer_rows(inputs, CONFIG, 2026, ["P0", "NEG", "GONE"], {}, problems)
    assert any("P0" in n and "GONE (fundamentals_cache_missing)" in n and "ELSE" not in n for n in notes)
    assert any("NEG" in n and "EV/EBITDA ungueltig" in n for n in notes)
    assert any("GONE" in n and "keine nutzbaren Fundamentaldaten" in n for n in notes)


def test_build_inputs_aborts_on_duplicate_tickers_and_uncovered_sector_codes(monkeypatch, tmp_path):
    monkeypatch.setattr(rep.run_stage2, "load_prefilter_rows", lambda db, ts: ("t", [(1, "AAA", 110001, 1e9), (2, "AAA", 110001, 1e9)]))
    with pytest.raises(rep.PeerExportError, match="mehreren simfin_id"):
        rep.build_inputs(tmp_path / "x.db", "t", __import__("datetime").date(2026, 9, 24), tmp_path, tmp_path)
    monkeypatch.setattr(rep.run_stage2, "load_prefilter_rows", lambda db, ts: ("t", [(1, "AAA", 1, 1e9)]))
    with pytest.raises(rep.PeerExportError, match="Branchenprofile unvollstaendig"):
        rep.build_inputs(tmp_path / "x.db", "t", __import__("datetime").date(2026, 9, 24), tmp_path, tmp_path)

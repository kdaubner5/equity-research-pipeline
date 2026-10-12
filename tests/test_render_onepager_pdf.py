"""render_onepager_pdf.py: PDF-Fassung des One-Pagers aus YAML (nur Darstellung)."""

import csv
from pathlib import Path

import pytest
import yaml

import render_onepager_pdf as r

REAL_YAML = Path(__file__).resolve().parents[1] / "docs" / "onepager" / "PPC.yaml"


def write_peers(path, ticker="PPC", n=6):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["candidate", "peer_group", "peer_level", "ticker", "status", "ev_ebitda",
                                                    "ev_ebitda_percentile", "fcf_yield", "peer_rank", "peer_n", "invalid_reasons"])
        writer.writeheader()
        for i in range(n):
            writer.writerow({"candidate": ticker, "peer_group": 102002, "peer_level": "fine", "ticker": ticker if i == 0 else f"P{i}",
                             "status": "passed", "ev_ebitda": 4.0 + i, "ev_ebitda_percentile": "", "fcf_yield": 0.04 + 0.01 * i,
                             "peer_rank": i + 1, "peer_n": n, "invalid_reasons": ""})
        writer.writerow({"candidate": "OTHER", "peer_group": 1, "peer_level": "fine", "ticker": "X", "status": "passed",
                         "ev_ebitda": 1.0, "ev_ebitda_percentile": "", "fcf_yield": 0.1, "peer_rank": 1, "peer_n": 1, "invalid_reasons": ""})


def test_the_real_yaml_loads_and_has_all_required_parts():
    data = r.load_yaml(REAL_YAML)
    assert data["company"]["ticker"] == "PPC" and len(data["kpis"]) == 5 and len(data["events"]) >= 5


def test_missing_parts_and_unknown_status_are_errors(tmp_path):
    bad = tmp_path / "x.yaml"
    bad.write_text("company: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fehlen"):
        r.load_yaml(bad)
    data = yaml.safe_load(REAL_YAML.read_text(encoding="utf-8"))
    data["kpis"][0]["status"] = "gruen"
    bad.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="Unbekannter Status"):
        r.load_yaml(bad)


def test_main_writes_a_pdf_without_peers_and_refuses_to_overwrite(tmp_path, capsys):
    out = tmp_path / "PPC.pdf"
    assert r.main([str(REAL_YAML), "--out", str(out)]) == 0
    assert out.read_bytes()[:5] == b"%PDF-" and out.stat().st_size > 5000
    assert "ohne Peer-Vergleich" in capsys.readouterr().out
    assert r.main([str(REAL_YAML), "--out", str(out)]) == 2


def test_main_with_peers_filters_by_candidate_and_builds_a_larger_pdf(tmp_path):
    peers = tmp_path / "peers.csv"
    write_peers(peers)
    rows = r.read_peers(peers, "PPC")
    assert len(rows) == 6 and all(p["candidate"] == "PPC" for p in rows)
    plain, with_peers = tmp_path / "a.pdf", tmp_path / "b.pdf"
    assert r.main([str(REAL_YAML), "--out", str(plain)]) == 0
    assert r.main([str(REAL_YAML), "--out", str(with_peers), "--peers", str(peers)]) == 0
    assert with_peers.stat().st_size > plain.stat().st_size


def test_peer_chart_needs_at_least_two_values():
    png, note = r.peer_chart([{"ticker": "PPC", "ev_ebitda": 4.0, "status": "passed"}], "PPC", "ev_ebitda", "t", r._num, True)
    assert png is None and "nur 1 von 1" in note


def test_main_reports_a_broken_yaml_instead_of_a_traceback(tmp_path, capsys):
    bad = tmp_path / "x.yaml"
    bad.write_text("company: {}\n", encoding="utf-8")
    assert r.main([str(bad), "--out", str(tmp_path / "o.pdf")]) == 2
    assert "ABBRUCH" in capsys.readouterr().err


def test_number_helpers_use_the_decimal_comma():
    assert r._pct(0.0463) == "4,6 %" and r._num(6.44) == "6,4" and r._usd(21.31) == "21,31 $"


def peer_rows(n=5, **extra):
    return [{"candidate": "PPC", "peer_group": 1, "peer_level": "fine", "ticker": "PPC" if i == 0 else f"P{i}", "status": "passed",
             "ev_ebitda": 4.0 + i, "fcf_yield": 0.05 + 0.01 * i, "invalid_reasons": "", "peer_n": n, **extra} for i in range(n)]


def test_peer_chart_counts_hidden_and_failed_firms_and_names_the_median():
    rows = peer_rows(6)
    rows[2]["invalid_reasons"] = "ebitda_negative"
    rows[3]["status"] = "failed"
    rows[4]["ev_ebitda"] = None
    png, note = r.peer_chart(rows, "PPC", "ev_ebitda", "t", r._num, True)
    assert png is not None
    assert "5 von 6 Firmen der Gruppe gezeigt" in note and "1 ohne Wert" in note and "1 in Stufe 2 durchgefallen" in note


def test_read_peers_checks_columns_candidate_level_and_bom(tmp_path):
    good = tmp_path / "g.csv"
    write_peers(good)
    good.write_bytes(b"\xef\xbb\xbf" + good.read_bytes())
    assert len(r.read_peers(good, "PPC")) == 6
    with pytest.raises(ValueError, match="keine Zeilen"):
        r.read_peers(good, "NOPE")
    wrong = tmp_path / "w.csv"
    wrong.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="erwarteten Spalten"):
        r.read_peers(wrong, "PPC")
    mixed = tmp_path / "m.csv"
    write_peers(mixed)
    text = mixed.read_text(encoding="utf-8")
    mixed.write_text(text + "PPC,102002,coarse,ZZZ,passed,1.0,,0.2,1,1,\n", encoding="utf-8")
    assert "ZZZ" not in [p["ticker"] for p in r.read_peers(mixed, "PPC")]


def test_nested_required_fields_and_main_never_leave_a_partial_pdf(tmp_path, capsys):
    data = yaml.safe_load(REAL_YAML.read_text(encoding="utf-8"))
    del data["entry"]["limit"]
    bad = tmp_path / "x.yaml"
    bad.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    out = tmp_path / "o.pdf"
    assert r.main([str(bad), "--out", str(out)]) == 2
    assert "In entry fehlen: limit" in capsys.readouterr().err and not out.exists()
    assert r.main([str(REAL_YAML), "--out", str(tmp_path / "fehlt" / "o.pdf")]) == 2
    assert not list(tmp_path.glob("*.teil"))


def test_a_note_on_a_kpi_card_is_part_of_the_pdf_and_the_entry_box_has_no_hidden_calculation():
    import inspect
    assert "/ entry[" not in inspect.getsource(r._entry_box)


def test_incomplete_peer_group_shows_no_group_median_and_says_so():
    rows = peer_rows(2)
    rows[0]["peer_n"] = 17
    png, note = r.peer_chart(rows, "PPC", "ev_ebitda", "t", r._num, True)
    assert png is not None and "2 von 17 Firmen der Gruppe gezeigt" in note and "kein Gruppenmedian" in note


def test_a_large_group_is_shown_as_a_window_around_the_candidate_with_the_median_of_all():
    rows = peer_rows(40)
    rows[0]["ticker"] = "PPC"
    png, note = r.peer_chart(rows, "PPC", "ev_ebitda", "t", r._num, True)
    assert png is not None and "Ausschnitt: 18 von 40 Firmen" in note and "Median über die ganze Gruppe" in note


def test_invalid_reasons_of_the_fcf_do_not_hide_a_valid_ev_ebitda():
    rows = peer_rows(4)
    rows[1]["invalid_reasons"] = "capex_missing"
    rows[1]["fcf_yield"] = None
    png_ev, note_ev = r.peer_chart(rows, "PPC", "ev_ebitda", "t", r._num, True)
    png_fcf, note_fcf = r.peer_chart(rows, "PPC", "fcf_yield", "t", r._pct, False)
    assert "4 von 4" in note_ev and "3 von 4" in note_fcf


def test_funnel_gates_and_financials_are_drawn_and_a_broken_section_is_reported(tmp_path, capsys):
    data = yaml.safe_load(REAL_YAML.read_text(encoding="utf-8"))
    assert data["funnel"][0]["count"] > data["funnel"][-1]["count"] and len(data["gates"]) >= 5
    assert all(len(row["values"]) == len(data["financials"]["columns"]) for row in data["financials"]["rows"])
    del data["financials"]["columns"]
    bad = tmp_path / "x.yaml"
    bad.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert r.main([str(bad), "--out", str(tmp_path / "o.pdf")]) == 2
    assert "ABBRUCH" in capsys.readouterr().err and not (tmp_path / "o.pdf").exists()


def test_valuation_map_needs_three_firms_with_both_values():
    peers = peer_rows(2)
    png, note = r.valuation_map(peers, "PPC")
    assert png is None and "weniger als 3" in note
    png, note = r.valuation_map(peer_rows(5), "PPC")
    assert png is not None and "5 von 5 Firmen" in note

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
    assert r.peer_chart([{"ticker": "PPC", "ev_ebitda": 4.0}], "PPC", "ev_ebitda", "t", r._num, True) is None


def test_main_reports_a_broken_yaml_instead_of_a_traceback(tmp_path, capsys):
    bad = tmp_path / "x.yaml"
    bad.write_text("company: {}\n", encoding="utf-8")
    assert r.main([str(bad), "--out", str(tmp_path / "o.pdf")]) == 2
    assert "ABBRUCH" in capsys.readouterr().err


def test_number_helpers_use_the_decimal_comma():
    assert r._pct(0.0463) == "4,6 %" and r._num(6.44) == "6,4" and r._usd(21.31) == "21,31 $"

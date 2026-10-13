"""Alle One-Pager-Dateien (docs/onepager/*.yaml): ladbar, vollstaendig, passend zur Kandidatenliste Runde 1."""

import re
from pathlib import Path

import pytest

import render_onepager_pdf as r

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted(p for p in (ROOT / "docs" / "onepager").glob("*.yaml"))
VERDICTS = {"WEITERVERFOLGEN", "ZURÜCKSTELLEN", "VERWERFEN", "UNZUREICHENDE EVIDENZ"}


def list_tickers():
    tickers = {}
    for line in (ROOT / "docs" / "kandidaten" / "runde1.md").read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and cells[0].isdigit():
            tickers[cells[2]] = int(cells[0])
    return tickers


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_each_yaml_loads_and_has_a_proper_verdict_and_sources(path):
    data = r.load_yaml(path)
    assert data["company"]["ticker"] == path.stem
    assert data["verdict"]["label"] in VERDICTS
    assert data["verdict"]["text"].startswith("Vorschlag des Agenten")
    assert data["sources"] and data["not_checked"] and len(data["events"]) >= 3
    assert re.search(r"\d{2}\.\d{2}\.2026", data["dates"]["price"])


def test_the_candidate_list_is_fully_covered_with_the_right_numbers():
    tickers = list_tickers()
    have = {p.stem: r.load_yaml(p)["company"]["list_nr"] for p in FILES if p.stem in tickers}
    assert have == tickers

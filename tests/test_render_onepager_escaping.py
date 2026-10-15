"""render_onepager_pdf.py: ein nacktes "&" im Text (zum Beispiel "CD&R") darf nicht als "CD&R;" im PDF stehen."""

import ast
import html
from pathlib import Path

import pytest
import yaml
from reportlab.lib.styles import ParagraphStyle

import render_onepager_pdf as r

ONEPAGER_DIR = Path(__file__).resolve().parents[1] / "docs" / "onepager"
STYLE = ParagraphStyle("t")


def plain(text):
    """Text, so wie reportlab ihn auf die Seite setzt."""
    return r.Paragraph(text, STYLE).getPlainText()


@pytest.mark.parametrize("text, expected", [
    ("CD&R", "CD&amp;R"),
    ("Oil & Gas - E&P", "Oil &amp; Gas - E&amp;P"),
    ("S&amp;P 500", "S&amp;P 500"),  # schon geschrieben: nicht doppelt maskieren
    ("a&nbsp;b &lt; c &gt; d &quot;e&quot; &apos;f&apos;", "a&nbsp;b &lt; c &gt; d &quot;e&quot; &apos;f&apos;"),
    ("&#38; und &#x26;", "&#38; und &#x26;"),
    ("AT&T Corp", "AT&amp;T Corp"),
    ("von 2020 &ndash; 2025, 5 &euro;, &AMP; &auml;", "von 2020 &ndash; 2025, 5 &euro;, &AMP; &auml;"),  # jede geschriebene Entität
    ("&amp bla", "&amp;amp bla"),  # ohne Semikolon ist es keine Entität
    ("AT&T; Verizon", "AT&T; Verizon"),  # bekannte Grenze: Name plus Semikolon gilt als Entität
    ("<b>A&B</b>", "<b>A&amp;B</b>"),  # Auszeichnung des Skripts bleibt erhalten
    ("&&", "&amp;&amp;"),
    ("", ""),
    (None, None),  # reportlab teilt lange Absätze über self.__class__(None, ...)
])
def test_escape_ampersands(text, expected):
    assert r.escape_ampersands(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("Übernahmeangebot von CD&R und McKesson", "Übernahmeangebot von CD&R und McKesson"),
    ("Oil & Gas - E&P (SimFin-Branche 107001)", "Oil & Gas - E&P (SimFin-Branche 107001)"),
    ("gegen den S&amp;P 500 und den MSCI World", "gegen den S&P 500 und den MSCI World"),
    ("<b>CD&R-Beteiligung</b> durch McKesson", "CD&R-Beteiligung durch McKesson"),
    ("Net Debt/EBITDA > 2,5 bei AT&T", "Net Debt/EBITDA > 2,5 bei AT&T"),
    ("von 2020 &ndash; 2025", "von 2020 – 2025"),  # reportlab löst geschriebene Entitäten weiter selbst auf
])
def test_the_paragraph_shows_the_text_as_written(text, expected):
    assert plain(text) == expected


def test_a_long_paragraph_can_still_be_split_across_pages():
    para = r.Paragraph("CD&R und McKesson " * 300, STYLE)
    para.wrap(400, 1000)
    parts = para.split(400, 100)
    assert len(parts) == 2 and all(isinstance(p, r.Paragraph) for p in parts)
    assert plain("CD&R und McKesson") == "CD&R und McKesson"


def test_every_text_in_all_onepager_yamls_is_shown_as_written():
    """Alle Texte mit &, < oder > in docs/onepager/*.yaml: Ausgabe = Wortlaut (geschriebene Entitäten aufgelöst)."""
    files = sorted(ONEPAGER_DIR.glob("*.yaml"))
    assert len(files) >= 40
    wrong, checked = [], 0

    def walk(node, path):
        if isinstance(node, str):
            yield path, node
        elif isinstance(node, dict):
            for key, value in node.items():
                yield from walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                yield from walk(value, f"{path}[{i}]")

    for file in files:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
        for path, text in walk(data, file.stem):
            if not any(ch in text for ch in "&<>"):
                continue
            checked += 1
            if " ".join(plain(text).split()) != " ".join(html.unescape(text).split()):
                wrong.append((path, text[:60]))
    assert checked > 40, "der Test prüft zu wenige Texte, die Suche nach &, < und > ist wohl kaputt"
    assert not wrong, f"{len(wrong)} Texte werden anders ausgegeben, als sie geschrieben sind: {wrong[:5]}"


def bare_ampersand_texts(path):
    """Alle Texte einer YAML mit einem nackten & (die Stellen, an denen das PDF früher "CD&R;" zeigte)."""
    found, stack = [], [yaml.safe_load(path.read_text(encoding="utf-8"))]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            if r._BARE_AMPERSAND.search(node):
                found.append(node)
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


@pytest.mark.parametrize("ticker, at_least", [("OPCH", 5), ("MTDR", 1)])
def test_every_text_with_a_bare_ampersand_goes_through_the_escaping_when_the_pdf_is_built(monkeypatch, tmp_path, ticker,
                                                                                         at_least):
    """Verdrahtung: Alle Absätze des PDFs müssen über r.Paragraph laufen. Die Eingaben von escape_ampersands werden
    mitgeschrieben; jeder Text mit nacktem & aus der YAML muss darin vorkommen (sonst ginge er an der Maskierung vorbei)."""
    yaml_path = ONEPAGER_DIR / f"{ticker}.yaml"
    texts = bare_ampersand_texts(yaml_path)
    assert len(texts) >= at_least, "die YAML hat keine Texte mit nacktem & mehr: der Test prüft nichts"
    seen, original = [], r.escape_ampersands
    monkeypatch.setattr(r, "escape_ampersands", lambda text: (seen.append(text), original(text))[1])
    out = tmp_path / f"{ticker}.pdf"
    assert r.main([str(yaml_path), "--out", str(out)]) == 0 and out.read_bytes()[:5] == b"%PDF-"
    missing = [text[:60] for text in texts if not any(text in s for s in seen if isinstance(s, str))]
    assert not missing, f"{len(missing)} Texte mit & laufen nicht über die Maskierung: {missing}"


def test_reportlabs_own_paragraph_is_only_the_base_class_of_ours():
    """Ein zweiter Weg zu reportlabs Paragraph (andere Import-Schreibweise, platypus.Paragraph(...)) würde die Maskierung umgehen."""
    tree = ast.parse(Path(r.__file__).read_text(encoding="utf-8"))
    imports = [(node.module, alias.name, alias.asname) for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
               for alias in node.names if alias.name == "Paragraph"]
    assert imports == [("reportlab.platypus", "Paragraph", "_ReportlabParagraph")]
    assert sum(1 for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "_ReportlabParagraph") == 1  # Basisklasse
    assert not any(isinstance(n, ast.Attribute) and n.attr == "Paragraph" for n in ast.walk(tree))
    assert r.Paragraph.__mro__[1].__module__.startswith("reportlab")

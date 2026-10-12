"""Erzeugt die PDF-Fassung eines One-Pagers aus einer YAML-Datei (Roadmap D.4, Wunsch von Konstantin, 12.10.2026).

Nur Darstellung: Das Skript rechnet keine Kennzahlen und kennt keine Schwellen. Alle Zahlen, Farben ("status") und das
Verdikt stehen in der YAML-Datei (Kurzfassung des Berichts in docs/onepager/). Optional wird eine Peer-CSV
(run_export_candidates.py --peers-out) fuer den Vergleich mit der Konkurrenz eingelesen; fehlt sie, steht an der Stelle
ein Hinweis statt eines Diagramms.

Aufruf (PowerShell, aus dem Projektordner):
    C:\\Users\\<Benutzer>\\anaconda3\\python.exe render_onepager_pdf.py docs\\onepager\\PPC.yaml --out data\\PPC.pdf
    ... mit Konkurrenz:   --peers data\\peers_A.csv

Exit-Code: 0 = PDF geschrieben; 2 = Abbruch (YAML unvollstaendig, Zieldatei existiert schon).
"""

import argparse
import csv
import io
import statistics
import sys
from pathlib import Path

import matplotlib
import matplotlib.ticker

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import yaml  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.enums import TA_LEFT  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.ttfonts import TTFont  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

_FONT_DIR = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
pdfmetrics.registerFont(TTFont("DV", str(_FONT_DIR / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DV-B", str(_FONT_DIR / "DejaVuSans-Bold.ttf")))
pdfmetrics.registerFontFamily("DV", normal="DV", bold="DV-B", italic="DV", boldItalic="DV-B")

INK = colors.HexColor("#1f2933")
MUTED = colors.HexColor("#6b7785")
LINE = colors.HexColor("#d5dbe1")
BG = colors.HexColor("#f4f6f8")
TONES = {  # Farbe je Status; die Zuordnung steht in der YAML-Datei
    "ok": colors.HexColor("#2e8b57"),
    "warn": colors.HexColor("#e08a00"),
    "bad": colors.HexColor("#c0392b"),
    "neutral": colors.HexColor("#4a6fa5"),
}
HEX = {k: "#%02x%02x%02x" % tuple(int(c * 255) for c in v.rgb()) for k, v in TONES.items()}

REQUIRED = ["company", "dates", "verdict", "thesis", "kpis", "events", "bear", "bull", "entry", "kill", "sources", "not_checked"]


def _style(name, size=8.5, leading=11.5, color=INK, bold=False, **kw):
    return ParagraphStyle(name, fontName="DV-B" if bold else "DV", fontSize=size, leading=leading, textColor=color,
                          alignment=TA_LEFT, **kw)


S_TITLE = _style("title", 17, 21, bold=True)
S_SUB = _style("sub", 8, 11, MUTED)
S_H = _style("h", 10, 13, bold=True, spaceBefore=6, spaceAfter=3)
S_BODY = _style("body", 8.5, 11.5)
S_SMALL = _style("small", 7, 9.5, MUTED)
S_BUL = _style("bul", 8, 10.8, leftIndent=8, bulletIndent=0)
S_KPI_L = _style("kl", 7, 9, MUTED)
S_KPI_V = _style("kv", 15, 18, bold=True)
S_KPI_S = _style("ks", 6.8, 8.8, MUTED)


def load_yaml(path):
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED if k not in (data or {})]
    if missing:
        raise ValueError("In der YAML-Datei fehlen: " + ", ".join(missing))
    for kpi in data["kpis"]:
        if kpi.get("status") not in TONES:
            raise ValueError(f"Unbekannter Status {kpi.get('status')!r} bei {kpi.get('label')!r}")
    if data["verdict"].get("tone") not in TONES:
        raise ValueError("verdict.tone muss ok, warn, bad oder neutral sein")
    return data


def read_peers(path, candidate):
    """Zeilen der Peer-CSV fuer einen Kandidaten als Liste von Dicts (leere Zahlen werden None)."""
    rows = []
    with open(path, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["candidate"] != candidate:
                continue
            for key in ("ev_ebitda", "fcf_yield", "ev_ebitda_percentile"):
                row[key] = float(row[key]) if row.get(key) not in (None, "") else None
            rows.append(row)
    return rows


def _png(fig):
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    buffer.seek(0)
    return buffer


def _style_axes(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#d5dbe1")
    ax.tick_params(colors="#6b7785", labelsize=7)


def halfyear_chart(data):
    hy = data["halfyear"]
    fig, ax = plt.subplots(figsize=(3.4, 2.2))
    width = 0.36
    names = [r["name"] for r in hy["rows"]]
    for i, label in enumerate(hy["labels"]):
        vals = [r["values"][i] for r in hy["rows"]]
        pos = [j + (i - 0.5) * width for j in range(len(names))]
        bars = ax.bar(pos, vals, width, color="#9fb3c8" if i == 0 else HEX["bad"], label=label)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v, f"{v:,.0f}".replace(",", "."), ha="center", va="bottom", fontsize=6.5, color="#1f2933")
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, fontsize=7)
    ax.set_ylabel(hy.get("unit", ""), fontsize=7, color="#6b7785")
    ax.legend(fontsize=6.5, frameon=False, loc="upper right")
    _style_axes(ax)
    ax.set_title("Halbjahr gegen Vorjahr", fontsize=8, loc="left", color="#1f2933")
    return _png(fig)


def peer_chart(peers, ticker, key, title, fmt, lower_is_better):
    rows = [p for p in peers if p[key] is not None]
    if len(rows) < 2:
        return None
    rows.sort(key=lambda p: p[key])
    values = [p[key] for p in rows]
    med = statistics.median(values)
    fig, ax = plt.subplots(figsize=(3.4, max(1.6, 0.17 * len(rows) + 0.7)))
    colors_ = [HEX["warn"] if p["ticker"] == ticker else "#b8c4d0" for p in rows]
    ax.barh([p["ticker"] for p in rows], values, color=colors_)
    ax.axvline(med, color="#1f2933", linewidth=0.8, linestyle="--")
    ax.annotate("Median " + fmt(med), xy=(med, 1.0), xycoords=("data", "axes fraction"), xytext=(0, 2), textcoords="offset points",
                fontsize=6.5, ha="center", va="bottom", color="#1f2933")
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, _pos: fmt(x)))
    ax.tick_params(axis="y", labelsize=6)
    ax.invert_yaxis()
    _style_axes(ax)
    ax.set_title(title + (" (niedriger = günstiger)" if lower_is_better else " (höher = günstiger)"), fontsize=7.5, loc="left", pad=14)
    return _png(fig)


def _dec(x, digits=1):
    return f"{x:.{digits}f}".replace(".", ",")


def _usd(x):
    return _dec(x, 2) + " $"


def _pct(x):
    return f"{x * 100:.1f} %".replace(".", ",")


def _num(x):
    return f"{x:.1f}".replace(".", ",")


def _kpi_cards(kpis):
    cells = []
    for k in kpis:
        tone = TONES[k["status"]]
        inner = Table(
            [[Paragraph(k["label"].upper(), S_KPI_L)], [Paragraph(k["main"], _style("kv" + k["label"], 15 if len(k["main"]) <= 7 else 12, 18, tone, bold=True))],
             [Paragraph(k["sub"], S_KPI_S)]],
            colWidths=[33 * mm],
        )
        inner.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                                   ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
        cells.append(inner)
    table = Table([cells], colWidths=[35 * mm] * len(cells))
    style = [("BOX", (i, 0), (i, 0), 0.6, LINE) for i in range(len(cells))]
    style += [("BACKGROUND", (i, 0), (i, 0), BG) for i in range(len(cells))]
    style += [("LINEBEFORE", (i, 0), (i, 0), 2.2, TONES[kpis[i]["status"]]) for i in range(len(cells))]
    style += [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    table.setStyle(TableStyle(style))
    return table


def _bullets(items, tone):
    return [Paragraph(f'<font color="{HEX[tone]}">●</font> {t}', _style("b" + tone, 8, 10.8, leftIndent=10, firstLineIndent=-10))
            for t in items]


def _timeline(events):
    rows = []
    for e in events:
        future = e.get("future")
        color = HEX["neutral"] if future else "#1f2933"
        rows.append([Paragraph(f'<font color="{color}"><b>{e["date"]}</b></font>', _style("td", 7.5, 10)),
                     Paragraph(e["text"] + (" <font color='#6b7785'>(offen)</font>" if future else ""), _style("tt", 7.5, 10))])
    table = Table(rows, colWidths=[20 * mm, 150 * mm])
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 1.2),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2), ("LEFTPADDING", (0, 0), (-1, -1), 2),
                               ("LINEBELOW", (0, 0), (-1, -2), 0.25, LINE)]))
    return table


def _entry_box(entry):
    gap = (entry["price"] / entry["limit_fcf"] - 1) * 100 if entry.get("limit_fcf") else None
    lines = [
        Paragraph(entry["zone_title"], _style("et", 9, 12, bold=True)),
        Paragraph(f'Zeithorizont: <b>{entry["horizon"]}</b>', S_BODY),
        Paragraph(f'Grenze nach FCF-Rendite 6 %: <b>{_usd(entry["limit_fcf"])}</b> '
                  f'(Kurs {_usd(entry["price"])} am {entry["price_date"]}'
                  + (f', {_dec(gap, 0)} % darüber' if gap is not None else "") + "). " + entry["limit_note"], S_BODY),
        Paragraph(f'Tauschwert JBS-Vorschlag: {_usd(entry["offer_value"])} am {entry["offer_date"]}.', S_BODY),
        Paragraph("<b>Bedingung:</b> " + entry["condition"], S_BODY),
    ]
    table = Table([[lines]], colWidths=[174 * mm])
    table.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, TONES["neutral"]), ("BACKGROUND", (0, 0), (-1, -1), BG),
                               ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                               ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return table


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("DV", 6.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 10 * mm, "Entwurf des Agenten. Keine Anlageberatung, keine Prognose, kein Kursziel. Entscheidung: Konstantin.")
    canvas.drawRightString(192 * mm, 10 * mm, f"Seite {doc.page}")
    canvas.restoreState()


def build(data, out_path, peers=None):
    c = data["company"]
    v = data["verdict"]
    story = []
    head = Table(
        [[Paragraph(f'{c["name"]} <font color="#6b7785" size="11">({c["ticker"]})</font>', S_TITLE),
          Paragraph(f'<font color="white"><b>{v["label"]}</b></font>', _style("vl", 10, 13, colors.white, bold=True))]],
        colWidths=[130 * mm, 44 * mm],
    )
    head.setStyle(TableStyle([("BACKGROUND", (1, 0), (1, 0), TONES[v["tone"]]), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                              ("ALIGN", (1, 0), (1, 0), "CENTER"), ("TOPPADDING", (1, 0), (1, 0), 6), ("BOTTOMPADDING", (1, 0), (1, 0), 6)]))
    story += [head, Spacer(1, 2)]
    story.append(Paragraph(
        f'Rang in Stufe 2: {c["stage2_rank"]} von {c["stage2_rank_of"]} · Kandidat Nr. {c["list_nr"]} · Peer-Gruppe {c["peer_group"]} · '
        f'Unterlagen bis {data["dates"]["documents_until"]} · Lauf {data["dates"]["archive_run"]} · One-Pager vom {data["dates"]["one_pager"]}', S_SUB))
    story.append(Paragraph("Kurs: " + data["dates"]["price"], S_SUB))
    story.append(Spacer(1, 4))
    story.append(Paragraph(v["text"], _style("vt", 8, 11, TONES[v["tone"]], bold=True)))
    story.append(Spacer(1, 4))
    story.append(_kpi_cards(data["kpis"]))
    story.append(Paragraph("Kernaussage", S_H))
    story.append(Paragraph(data["thesis"], S_BODY))

    charts = []
    if data.get("halfyear"):
        charts.append(Image(halfyear_chart(data), width=84 * mm, height=55 * mm))
    ticker = c["ticker"]
    peer_charts = []
    if peers:
        a = peer_chart(peers, ticker, "ev_ebitda", "EV/EBITDA der Peer-Gruppe", _num, True)
        b = peer_chart(peers, ticker, "fcf_yield", "FCF-Rendite der Peer-Gruppe", _pct, False)
        for png in (a, b):
            if png:
                peer_charts.append(Image(png, width=84 * mm, height=60 * mm, kind="proportional"))
    story.append(Paragraph("Einordnung", S_H))
    row = charts[:]
    if peer_charts:
        row.append(peer_charts[0])
    ctable = Table([row or [Paragraph("Keine Diagramme.", S_SMALL)]], colWidths=[87 * mm] * max(1, len(row)))
    ctable.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(ctable)
    if peers and len(peer_charts) > 1:
        story.append(Table([[peer_charts[1]]], colWidths=[87 * mm]))
    if not peers:
        story.append(Paragraph("Vergleich mit der Konkurrenz: Peer-Daten fehlen (Export mit run_export_candidates.py --peers-out). "
                               "Ohne sie steht hier kein Diagramm; die Einordnung gegen die Peer-Gruppe ist offen.", S_SMALL))
    story.append(Paragraph("Was ist passiert (Zeitleiste)", S_H))
    story.append(_timeline(data["events"]))
    story.append(Spacer(1, 6))
    cols = Table([[[Paragraph("Bear: Risiken", _style("bh", 9.5, 12, TONES["bad"], bold=True))] + _bullets(data["bear"], "bad"),
                   [Paragraph("Bull: Argumente dafür", _style("gh", 9.5, 12, TONES["ok"], bold=True))] + _bullets(data["bull"], "ok")]],
                 colWidths=[87 * mm, 87 * mm])
    cols.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("RIGHTPADDING", (0, 0), (0, 0), 8)]))
    story.append(cols)
    story.append(Spacer(1, 6))
    story.append(KeepTogether([_entry_box(data["entry"])]))
    story.append(Spacer(1, 4))
    story.append(Paragraph("Ausstieg (Kill-Kriterium)", S_H))
    story.append(Paragraph(data["kill"], S_BODY))
    story.append(Paragraph("Quellen", S_H))
    story.append(Paragraph(data["sources"], S_SMALL))
    story.append(Paragraph("Nicht geprüft", S_H))
    story.append(Paragraph(data["not_checked"], S_SMALL))

    doc = SimpleDocTemplate(str(out_path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=14 * mm,
                            bottomMargin=16 * mm, title=f'One-Pager {c["ticker"]}', author="Equity-Research (Agent)")
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("yaml_path")
    parser.add_argument("--out", required=True)
    parser.add_argument("--peers")
    args = parser.parse_args(argv)
    if Path(args.out).exists():
        print(f"ABBRUCH: Zieldatei existiert schon (wird nicht überschrieben): {args.out}", file=sys.stderr)
        return 2
    try:
        data = load_yaml(args.yaml_path)
        peers = read_peers(args.peers, data["company"]["ticker"]) if args.peers else None
    except (ValueError, OSError, yaml.YAMLError, KeyError) as exc:
        print(f"ABBRUCH: {exc}", file=sys.stderr)
        return 2
    build(data, args.out, peers)
    print(f"PDF geschrieben: {args.out}" + ("" if peers else " (ohne Peer-Vergleich)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

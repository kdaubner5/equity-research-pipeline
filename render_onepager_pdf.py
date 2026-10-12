"""Erzeugt die PDF-Fassung eines One-Pagers aus einer Textdatei (Roadmap D.4, Wunsch von Konstantin, 12.10.2026).

In einfachen Worten: Das Skript zeichnet nur, was in der Textdatei (zum Beispiel docs/onepager/PPC.yaml) steht: Zahlen,
Farben, Verdikt und Texte. Es bewertet nichts. Fehlt in der Datei etwas Wichtiges, bricht es mit einer Meldung ab und
schreibt keine halbfertige PDF. Einzige Rechnung im Skript: der Median der Vergleichsfirmen im Konkurrenz-Diagramm
(Mitte aller gezeigten Werte).

Optional liest es eine Datei mit den Vergleichsfirmen (Peers), die run_export_candidates.py mit --peers-out erzeugt. Ohne
diese Datei steht an der Stelle des Diagramms ein Hinweis. Ist die Datei angegeben, aber unbrauchbar, bricht es ab.

Aufruf (PowerShell, im Projektordner; python ist der Pfad zu deinem Anaconda-Python):
    python render_onepager_pdf.py docs\\onepager\\PPC.yaml --out data\\PPC.pdf
    python render_onepager_pdf.py docs\\onepager\\PPC.yaml --out data\\PPC.pdf --peers data\\peers_A.csv

Ausgang: 0 = PDF geschrieben; 2 = Abbruch (Datei unvollstaendig oder nicht lesbar, Zieldatei existiert schon).
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

REQUIRED_NESTED = {
    "entry": ["zone_title", "horizon", "limit", "limit_label", "limit_note", "price", "price_date", "price_vs_limit",
              "offer_value", "offer_date", "condition"],
    "company": ["name", "ticker", "list_nr", "stage2_rank", "stage2_rank_of", "peer_group"],
    "dates": ["documents_until", "one_pager", "archive_run", "price"],
    "verdict": ["label", "tone", "text"],
}
REQUIRED = ["company", "dates", "verdict", "thesis", "kpis", "events", "bear", "bull", "entry", "kill", "sources", "not_checked"]


def _style(name, size=8.5, leading=11.5, color=INK, bold=False, **kw):
    return ParagraphStyle(name, fontName="DV-B" if bold else "DV", fontSize=size, leading=leading, textColor=color,
                          alignment=TA_LEFT, **kw)


S_TITLE = _style("title", 17, 21, bold=True)
S_SUB = _style("sub", 8, 11, MUTED)
S_H = _style("h", 10, 13, bold=True, spaceBefore=6, spaceAfter=3, keepWithNext=1)
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
    for section, keys in REQUIRED_NESTED.items():
        absent = [k for k in keys if data[section].get(k) in (None, "")]
        if absent:
            raise ValueError(f"In {section} fehlen: " + ", ".join(absent))
    for i, kpi in enumerate(data["kpis"], start=1):
        absent = [k for k in ("label", "main", "sub") if not kpi.get(k)]
        if absent:
            raise ValueError(f"Karte {i} (kpis): es fehlen " + ", ".join(absent))
    for i, event in enumerate(data["events"], start=1):
        if not event.get("date") or not event.get("text"):
            raise ValueError(f"Ereignis {i} (events): Datum oder Text fehlt")
    for key in ("bear", "bull"):
        if not data[key]:
            raise ValueError(f"{key}: Liste ist leer")
    hy = data.get("halfyear")
    if hy:
        if len(hy.get("labels", [])) != 2 or not hy.get("rows") or any(len(r.get("values", [])) != 2 for r in hy["rows"]):
            raise ValueError("halfyear: genau zwei Beschriftungen und je Zeile zwei Werte erwartet")
    return data


PEER_COLUMNS = ["candidate", "peer_group", "peer_level", "ticker", "status", "ev_ebitda", "ev_ebitda_percentile", "fcf_yield",
                "peer_rank", "peer_n", "invalid_reasons"]


def read_peers(path, candidate):
    """Zeilen der Peer-CSV fuer einen Kandidaten. Bricht bei fehlenden Spalten ab. Leere Zahlen werden None (und im
    Diagramm gezaehlt, nicht verschwiegen)."""
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in PEER_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError("Die Peer-Datei hat nicht die erwarteten Spalten, es fehlen: " + ", ".join(missing))
        for row in reader:
            if row["candidate"] != candidate:
                continue
            for key in ("ev_ebitda", "fcf_yield", "ev_ebitda_percentile"):
                row[key] = float(row[key]) if row.get(key) not in (None, "") else None
            rows.append(row)
    if not rows:
        raise ValueError(f"Die Peer-Datei enthaelt keine Zeilen fuer {candidate}.")
    own = [r for r in rows if r["ticker"] == candidate]
    if own:  # nur die Firmen derselben Gruppe und Ebene wie der Kandidat
        rows = [r for r in rows if (r["peer_group"], r["peer_level"]) == (own[0]["peer_group"], own[0]["peer_level"])]
    seen, unique = set(), []
    for r in rows:
        if r["ticker"] not in seen:
            seen.add(r["ticker"])
            unique.append(r)
    return unique


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
        bars = ax.bar(pos, vals, width, color="#9fb3c8" if i == 0 else HEX[hy.get("tone", "bad")], label=label)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v, f"{v:,.1f}".replace(",", "#").replace(".", ",").replace("#", "."), ha="center", va="bottom", fontsize=6.5, color="#1f2933")
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, fontsize=7)
    ax.set_ylabel(hy.get("unit", ""), fontsize=7, color="#6b7785")
    ax.legend(fontsize=6.5, frameon=False, loc="upper right")
    _style_axes(ax)
    ax.set_title("Halbjahr gegen Vorjahr", fontsize=8, loc="left", color="#1f2933")
    return _png(fig)


MAX_BARS = 18
FAILED_STATUS = ("failed", "not_evaluable")


def _group_size(peers, ticker):
    """Groesse der Peer-Gruppe laut Archiv (peer_n der eigenen Zeile des Kandidaten), sonst None."""
    for p in peers:
        if p["ticker"] == ticker and p.get("peer_n") not in (None, ""):
            return int(float(p["peer_n"]))
    return None


def shown_rows(peers, ticker, key, lower_is_better):
    """Die im Balkendiagramm gezeigten Zeilen (sortiert, bei grossen Gruppen ein Ausschnitt um den Kandidaten) und alle Zeilen mit Wert."""
    rows = [p for p in peers if p[key] is not None]
    rows.sort(key=lambda p: p[key], reverse=not lower_is_better)
    total = len(rows)
    if total > MAX_BARS:
        index = next((i for i, p in enumerate(rows) if p["ticker"] == ticker), total // 2)
        start = max(0, min(index - MAX_BARS // 2, total - MAX_BARS))
        return rows[start:start + MAX_BARS], rows
    return rows, rows


def peer_chart(peers, ticker, key, title, fmt, lower_is_better):
    """Balkendiagramm der im Archiv gespeicherten Vergleichsfirmen. Firmen mit Ungueltigkeitsgrund oder ohne Wert werden
    nicht gezeichnet, aber in der Unterzeile gezaehlt. Das Archiv speichert nur die Firmen, die Stufe 1 bestanden haben;
    ist die Gruppe nach peer_n groesser, gibt es KEINEN Median (er waere nicht der der Gruppe). Gibt (Bild oder None,
    Hinweistext) zurueck."""
    stored = len(peers)
    group_n = _group_size(peers, ticker)
    rows = [p for p in peers if p[key] is not None]
    hidden = stored - len(rows)
    if len(rows) < 2:
        return None, f"{title}: nur {len(rows)} von {stored} gespeicherten Firmen mit Wert, kein Diagramm."
    rows, all_rows = shown_rows(peers, ticker, key, lower_is_better)  # grosse Gruppe: Ausschnitt, Median ueber alle
    values = [p[key] for p in all_rows]
    complete = group_n is None or stored >= group_n
    failed = sum(1 for p in all_rows if p.get("status") in FAILED_STATUS)
    total_rows = len(all_rows)
    fig, ax = plt.subplots(figsize=(3.4, max(1.6, 0.17 * len(rows) + 0.9)))
    colors_ = [HEX["warn"] if p["ticker"] == ticker else ("#e3c4c4" if p.get("status") in FAILED_STATUS else "#b8c4d0")
               for p in rows]
    ax.barh([p["ticker"] for p in rows], [p[key] for p in rows], color=colors_)
    if complete:
        med = statistics.median(values)
        ax.axvline(med, color="#1f2933", linewidth=0.8, linestyle="--")
        ax.annotate("Median " + fmt(med), xy=(med, 1.0), xycoords=("data", "axes fraction"), xytext=(0, 2), textcoords="offset points",
                    fontsize=6.5, ha="center", va="bottom", color="#1f2933")
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, _pos: fmt(x)))
    ax.tick_params(axis="y", labelsize=6)
    ax.invert_yaxis()
    _style_axes(ax)
    ax.set_title(title + (" (niedriger = günstiger)" if lower_is_better else " (höher = günstiger)"), fontsize=7.5, loc="left", pad=14)
    note = f"{len(rows)} von {group_n if group_n else stored} Firmen der Gruppe gezeigt" if total_rows == len(rows) else \
        f"Ausschnitt: {len(rows)} von {group_n if group_n else stored} Firmen um den Kandidaten (Median über die ganze Gruppe)"
    if not complete:
        note += " (nur diese sind im Archiv gespeichert, kein Gruppenmedian)"
    if hidden:
        note += f"; {hidden} ohne Wert (zum Beispiel zu wenige Jahre oder fehlende Investitionen)"
    if failed:
        note += f"; {failed} in Stufe 2 durchgefallen (rosa)"
    return _png(fig), note + "."


def funnel_chart(funnel):
    """Trichter: Balken mit der Zahl der Firmen je Stufe (lineare Breite, Beschriftung umgebrochen)."""
    import textwrap
    labels = ["\n".join(textwrap.wrap(f["label"], 30)) for f in funnel]
    counts = [f["count"] for f in funnel]
    fig, ax = plt.subplots(figsize=(3.6, 2.6))
    y = list(range(len(funnel)))
    ax.barh(y, counts, color=["#b8c4d0"] * (len(funnel) - 1) + [HEX["warn"]], height=0.62)
    top = max(counts)
    for i, c in enumerate(counts):
        ax.text(c + top * 0.02, i, f"{c:,}".replace(",", "."), va="center", fontsize=8, color="#1f2933", fontweight="bold")
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=7)
    ax.invert_yaxis()
    ax.set_xlim(0, top * 1.22)
    ax.set_xticks([])
    for side in ("top", "right", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color("#d5dbe1")
    ax.tick_params(left=False)
    return _png(fig)


def valuation_map(peers, ticker, group_label=""):
    """Streudiagramm EV/EBITDA gegen FCF-Rendite der Peer-Gruppe; Ausreisser ausserhalb des gezeigten Bereichs werden gezaehlt."""
    pts = [p for p in peers if p["ev_ebitda"] is not None and p["fcf_yield"] is not None]
    if len(pts) < 3:
        return None, "Bewertungskarte: weniger als 3 Firmen mit beiden Werten."
    shown = [p for p in pts if p["ev_ebitda"] <= 40 and -0.1 <= p["fcf_yield"] <= 0.35]
    outside = len(pts) - len(shown)
    fig, ax = plt.subplots(figsize=(3.4, 2.6))
    for p in shown:
        own = p["ticker"] == ticker
        ax.scatter(p["ev_ebitda"], p["fcf_yield"] * 100, s=46 if own else 16, color=HEX["warn"] if own else "#8da2b8",
                   edgecolors="white", linewidths=0.4, zorder=3 if own else 2)
        if own:
            ax.annotate(ticker, (p["ev_ebitda"], p["fcf_yield"] * 100), xytext=(4, 4), textcoords="offset points", fontsize=7, fontweight="bold")
    ax.set_xlabel("EV/EBITDA (niedriger = günstiger)", fontsize=6.8, color="#6b7785")
    ax.set_ylabel("FCF-Rendite in % (höher = günstiger)", fontsize=6.8, color="#6b7785")
    _style_axes(ax)
    ax.set_title("Bewertungskarte der Peer-Gruppe (links oben = günstig)", fontsize=7.5, loc="left")
    note = f"Bewertungskarte: {len(shown)} von {len(pts)} Firmen im gezeigten Bereich" + (f" ({outside} Ausreißer außerhalb)" if outside else "") + "."
    return _png(fig), note


def _fin_table(fin):
    cols = fin["columns"]
    head = [""] + cols
    data = [head] + [[r["name"]] + list(r["values"]) for r in fin["rows"]]
    widths = [46 * mm] + [(174 - 46) / len(cols) * mm] * len(cols)
    table = Table([[Paragraph(str(c), _style("fh", 7, 9, bold=True)) if i == 0 or j == 0 else Paragraph(str(c), _style("fv", 7.5, 9.5, bold=(i == 0)))
                    for j, c in enumerate(row)] for i, row in enumerate(data)], colWidths=widths)
    style = [("BACKGROUND", (0, 0), (-1, 0), BG), ("LINEBELOW", (0, 0), (-1, -1), 0.25, LINE), ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
             ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("LINEAFTER", (3, 0), (3, -1), 0.8, LINE)]
    table.setStyle(TableStyle(style))
    return table


def _gates_table(gates):
    rows = []
    for g in gates:
        tone = HEX[g.get("status", "neutral")]
        rows.append([Paragraph(f'<font color="{tone}">●</font> <b>{g["stage"]}</b>', _style("gs", 6.8, 8.6)),
                     Paragraph(g["name"], _style("gn", 6.8, 8.6)),
                     Paragraph(f'<b>{g["value"]}</b>', _style("gv", 6.8, 8.6)),
                     Paragraph(g["rule"], _style("gr", 6.3, 8, MUTED))])
    table = Table(rows, colWidths=[18 * mm, 27 * mm, 19 * mm, 22 * mm])
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.25, LINE),
                               ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
                               ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
    return table


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
             [Paragraph(k["sub"], S_KPI_S)]] + ([[Paragraph(k["note"], _style("kn", 6.3, 8, TONES[k["status"]]))]] if k.get("note") else []),
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
    lines = [
        Paragraph(entry["zone_title"], _style("et", 9, 12, bold=True)),
        Paragraph(f'Zeithorizont: <b>{entry["horizon"]}</b>', S_BODY),
        Paragraph(f'{entry["limit_label"]}: <b>{_usd(entry["limit"])}</b> (Kurs {_usd(entry["price"])} am {entry["price_date"]}, '
                  f'{entry["price_vs_limit"]}). {entry["limit_note"]}', S_BODY),
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
    if v.get("legend"):
        story.append(Paragraph(v["legend"], S_SMALL))
    story.append(Spacer(1, 4))
    story.append(_kpi_cards(data["kpis"]))
    story.append(Paragraph("Kernaussage", S_H))
    story.append(Paragraph(data["thesis"], S_BODY))

    if data.get("funnel"):
        story.append(Paragraph(data.get("funnel_title", "Weg durch den Trichter"), S_H))
        left = Image(funnel_chart(data["funnel"]), width=84 * mm, height=60 * mm, kind="proportional")
        right = _gates_table(data["gates"]) if data.get("gates") else Spacer(1, 1)
        ft = Table([[left, right]], colWidths=[87 * mm, 87 * mm])
        ft.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(ft)
        if data.get("funnel_note"):
            story.append(Paragraph(data["funnel_note"], S_SMALL))
        if data.get("gates_note"):
            story.append(Paragraph(data["gates_note"], S_SMALL))
    if data.get("financials"):
        story.append(Paragraph(data.get("financials_title", "Kennzahlen"), S_H))
        story.append(_fin_table(data["financials"]))
        story.append(Paragraph(data["financials"].get("note", ""), S_SMALL))

    ticker = c["ticker"]
    grid, peer_notes = [], []
    if data.get("halfyear"):
        grid.append(Image(halfyear_chart(data), width=84 * mm, height=55 * mm, kind="proportional"))
    if peers:
        png, note = valuation_map(peers, ticker)
        peer_notes.append(note)
        if png:
            grid.append(Image(png, width=84 * mm, height=64 * mm, kind="proportional"))
        for key, title, fmt, lower in (("ev_ebitda", "EV/EBITDA der Peer-Gruppe", _num, True),
                                       ("fcf_yield", "FCF-Rendite der Peer-Gruppe", _pct, False)):
            png, note = peer_chart(peers, ticker, key, title, fmt, lower)
            peer_notes.append(note)
            if png:
                grid.append(Image(png, width=84 * mm, height=64 * mm, kind="proportional"))
    story.append(Paragraph("Einordnung gegen die Konkurrenz", S_H))
    rows = [grid[i:i + 2] + [""] * (2 - len(grid[i:i + 2])) for i in range(0, len(grid), 2)] or [[Paragraph("Keine Diagramme.", S_SMALL)]]
    gt = Table(rows, colWidths=[87 * mm, 87 * mm])
    gt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(gt)
    if not peers:
        story.append(Paragraph("Vergleich mit der Konkurrenz: Peer-Daten fehlen (Export mit run_export_peers.py). "
                               "Ohne sie steht hier kein Diagramm; die Einordnung gegen die Peer-Gruppe ist offen.", S_SMALL))
    else:
        own = next((p for p in peers if p["ticker"] == ticker), None)
        rank_line = ""
        if own and own.get("peer_rank") and own.get("peer_n"):
            rank_line = (f"Position in der Peer-Gruppe (EV/EBITDA, Stufe 2): Platz {int(float(own['peer_rank']))} von "
                         f"{int(float(own['peer_n']))}")
            if own.get("ev_ebitda_percentile") is not None:
                rank_line += f", {_dec(own['ev_ebitda_percentile'] * 100, 0)} % der Gruppe sind günstiger"
            rank_line += ". "
        if data.get("peer_summary"):
            story.append(Paragraph(data["peer_summary"], _style("ps", 8.5, 11.5, bold=True)))
        shown, _ = shown_rows(peers, ticker, "ev_ebitda", True)
        named = [f'{p["ticker"]} {p["name"]}' for p in shown if p.get("name")]
        if named:
            story.append(Paragraph("<b>Vergleichsfirmen (" + ("alle" if len(shown) == len(peers) else "Ausschnitt") + "):</b> " +
                                   " · ".join(named), S_SMALL))
        extra = (data.get("peer_note", "") + " ") if data.get("peer_note") else ""
        story.append(Paragraph(rank_line + " ".join(dict.fromkeys(peer_notes)) + " " + extra + "Die Diagramme zeigen die Archivwerte der "
                               "Stufe 2 (Geschäftsjahre bzw. 3-Jahres-Mittel); die Karten oben zeigen teils die letzten zwölf Monate.", S_SMALL))

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
    for line in data.get("sources_detail", []):
        story.append(Paragraph("• " + line, S_SMALL))
    story.append(Paragraph("Nicht geprüft", S_H))
    story.append(Paragraph(data["not_checked"], S_SMALL))
    if data.get("method"):
        story.append(Paragraph("Methode", S_H))
        story.append(Paragraph(data["method"], S_SMALL))

    doc = SimpleDocTemplate(str(out_path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=14 * mm,
                            bottomMargin=16 * mm, title=f'One-Pager {c["ticker"]}', author="Equity-Research (Agent)")
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("yaml_path")
    parser.add_argument("--out", required=True)
    parser.add_argument("--peers")
    args = parser.parse_args(argv)
    out = Path(args.out)
    if out.exists():
        print(f"ABBRUCH: Zieldatei existiert schon (wird nicht überschrieben): {args.out}", file=sys.stderr)
        return 2
    temp = out.with_name(out.name + ".teil")
    try:
        data = load_yaml(args.yaml_path)
        peers = read_peers(args.peers, data["company"]["ticker"]) if args.peers else None
        build(data, temp, peers)
        temp.replace(out)
    except (ValueError, OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        print(f"ABBRUCH: {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            temp.unlink()
        except OSError:
            pass
        return 2
    print(f"PDF geschrieben: {args.out}" + ("" if peers else " (ohne Peer-Vergleich)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

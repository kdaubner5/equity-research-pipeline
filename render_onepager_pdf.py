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
from reportlab.lib.enums import TA_LEFT, TA_RIGHT  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.ttfonts import TTFont  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    CondPageBreak, Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

_FONT_DIR = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
pdfmetrics.registerFont(TTFont("DV", str(_FONT_DIR / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DV-B", str(_FONT_DIR / "DejaVuSans-Bold.ttf")))
pdfmetrics.registerFontFamily("DV", normal="DV", bold="DV-B", italic="DV", boldItalic="DV-B")

INK = colors.HexColor("#1b2733")
MUTED = colors.HexColor("#667385")
LINE = colors.HexColor("#dde3ea")
BG = colors.HexColor("#f3f5f8")
NAVY = colors.HexColor("#14213d")
TONES = {  # Farbe je Status; die Zuordnung steht in der YAML-Datei
    "ok": colors.HexColor("#2e8b57"),
    "warn": colors.HexColor("#e08a00"),
    "bad": colors.HexColor("#c0392b"),
    "neutral": colors.HexColor("#4a6fa5"),
}
HEX = {k: "#%02x%02x%02x" % tuple(int(c * 255) for c in v.rgb()) for k, v in TONES.items()}
TINT = {"ok": "#eaf5ee", "warn": "#fdf3e1", "bad": "#fbebe8", "neutral": "#ebf0f7"}
CONTENT_W = 174  # mm

REQUIRED_NESTED = {
    "entry": ["zone_title", "horizon", "limit", "limit_label", "limit_note", "price", "price_date", "price_vs_limit",
              "condition"],
    "company": ["name", "ticker", "list_nr", "stage2_rank", "stage2_rank_of", "peer_group"],
    "dates": ["documents_until", "one_pager", "archive_run", "price"],
    "verdict": ["label", "tone", "text"],
}
REQUIRED = ["company", "dates", "verdict", "thesis", "kpis", "events", "bear", "bull", "entry", "kill", "sources", "not_checked"]


def _style(name, size=8.5, leading=11.5, color=INK, bold=False, **kw):
    kw.setdefault("alignment", TA_LEFT)
    return ParagraphStyle(name, fontName="DV-B" if bold else "DV", fontSize=size, leading=leading, textColor=color, **kw)


S_TITLE = _style("title", 22, 26, colors.white, bold=True)
S_SUB = _style("sub", 8.5, 12, colors.HexColor("#c9d3e3"))
S_H = _style("h", 14, 18, NAVY, bold=True, spaceBefore=14, spaceAfter=6, keepWithNext=1)
S_H2 = _style("h2", 10.5, 14, NAVY, bold=True, spaceBefore=10, spaceAfter=4, keepWithNext=1)
S_BODY = _style("body", 9.5, 14)
S_LEAD = _style("lead", 11, 16)
S_CARD = _style("card", 8.8, 12.4)
S_SMALL = _style("small", 8, 11.5, MUTED)
S_BUL = _style("bul", 9, 13, leftIndent=10, bulletIndent=0)
S_KPI_L = _style("kl", 7.5, 10, MUTED)
S_KPI_V = _style("kv", 20, 24, bold=True)
S_KPI_S = _style("ks", 7.5, 10, MUTED)


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
    ax.tick_params(colors="#667385", labelsize=8)


def halfyear_chart(data):
    hy = data["halfyear"]
    fig, ax = plt.subplots(figsize=(3.6, 2.7))
    width = 0.36
    names = [r["name"] for r in hy["rows"]]
    for i, label in enumerate(hy["labels"]):
        vals = [r["values"][i] for r in hy["rows"]]
        pos = [j + (i - 0.5) * width for j in range(len(names))]
        bars = ax.bar(pos, vals, width, color="#9fb3c8" if i == 0 else HEX[hy.get("tone", "bad")], label=label)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v, f"{v:,.1f}".replace(",", "#").replace(".", ",").replace("#", "."), ha="center", va="bottom", fontsize=8, color="#1b2733")
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, fontsize=8.5)
    ax.set_ylabel(hy.get("unit", ""), fontsize=8, color="#667385")
    ax.legend(fontsize=8, frameon=False, loc="upper right")
    _style_axes(ax)
    ax.set_title("Halbjahr gegen Vorjahr", fontsize=10, loc="left", color="#14213d", fontweight="bold", pad=8)
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
    fig, ax = plt.subplots(figsize=(3.6, max(2.0, 0.21 * len(rows) + 1.0)))
    colors_ = [HEX["warn"] if p["ticker"] == ticker else ("#e3c4c4" if p.get("status") in FAILED_STATUS else "#b8c4d0")
               for p in rows]
    ax.barh([p["ticker"] for p in rows], [p[key] for p in rows], color=colors_)
    if complete:
        med = statistics.median(values)
        ax.axvline(med, color="#1b2733", linewidth=1.0, linestyle="--")
        ax.annotate("Median " + fmt(med), xy=(med, 1.0), xycoords=("data", "axes fraction"), xytext=(0, 2), textcoords="offset points",
                    fontsize=8, ha="center", va="bottom", color="#1b2733")
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, _pos: fmt(x)))
    ax.tick_params(axis="y", labelsize=7.5)
    ax.invert_yaxis()
    _style_axes(ax)
    ax.set_title(title, fontsize=10, loc="left", pad=20, color="#14213d", fontweight="bold")
    ax.set_title("niedriger = günstiger" if lower_is_better else "höher = günstiger", fontsize=8, loc="right", pad=20, color="#667385")
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
    labels = ["\n".join(textwrap.wrap(f["label"], 46)) for f in funnel]
    counts = [f["count"] for f in funnel]
    fig, ax = plt.subplots(figsize=(7.0, 2.9))
    y = list(range(len(funnel)))
    ax.barh(y, counts, color=["#b8c4d0"] * (len(funnel) - 1) + [HEX["warn"]], height=0.62)
    top = max(counts)
    for i, c in enumerate(counts):
        ax.text(c + top * 0.02, i, f"{c:,}".replace(",", "."), va="center", fontsize=10, color="#1b2733", fontweight="bold")
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=8.5)
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
    fig, ax = plt.subplots(figsize=(3.6, 3.0))
    for p in shown:
        own = p["ticker"] == ticker
        ax.scatter(p["ev_ebitda"], p["fcf_yield"] * 100, s=70 if own else 24, color=HEX["warn"] if own else "#8da2b8",
                   edgecolors="white", linewidths=0.4, zorder=3 if own else 2)
        if own:
            ax.annotate(ticker, (p["ev_ebitda"], p["fcf_yield"] * 100), xytext=(5, 5), textcoords="offset points", fontsize=9, fontweight="bold")
    ax.set_xlabel("EV/EBITDA (niedriger = günstiger)", fontsize=8, color="#667385")
    ax.set_ylabel("FCF-Rendite in % (höher = günstiger)", fontsize=8, color="#667385")
    _style_axes(ax)
    ax.set_title("Bewertungskarte", fontsize=10, loc="left", color="#14213d", fontweight="bold", pad=12)
    ax.set_title("links oben = günstig", fontsize=8, loc="right", color="#667385", pad=12)
    note = f"Bewertungskarte: {len(shown)} von {len(pts)} Firmen im gezeigten Bereich" + (f" ({outside} Ausreißer außerhalb)" if outside else "") + "."
    return _png(fig), note


def _pad(table, left=0, right=0, top=0, bottom=0):
    table.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), left), ("RIGHTPADDING", (0, 0), (-1, -1), right),
                               ("TOPPADDING", (0, 0), (-1, -1), top), ("BOTTOMPADDING", (0, 0), (-1, -1), bottom)]))


def _card(flowables, tone="neutral", width=CONTENT_W, background=None):
    """Heller Kasten mit farbigem Balken links."""
    table = Table([[flowables]], colWidths=[width * mm])
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), background or colors.HexColor(TINT[tone])),
                               ("LINEBEFORE", (0, 0), (0, 0), 3, TONES[tone]),
                               ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                               ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]))
    return table


def _fin_table(fin):
    cols = fin["columns"]
    head = [""] + cols
    data = [head] + [[r["name"]] + list(r["values"]) for r in fin["rows"]]
    first = 54
    widths = [first * mm] + [(CONTENT_W - first) / len(cols) * mm] * len(cols)
    table = Table([[Paragraph(str(c), _style("fh", 8.5, 11, colors.white if i == 0 else INK, bold=True)) if i == 0 or j == 0
                    else Paragraph(str(c), _style("fv", 9, 12, colors.white if i == 0 else INK, bold=(i == 0), alignment=TA_RIGHT))
                    for j, c in enumerate(row)] for i, row in enumerate(data)], colWidths=widths)
    style = [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE), ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
             ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LEFTPADDING", (0, 0), (-1, -1), 6),
             ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("LINEAFTER", (3, 0), (3, -1), 1, LINE),
             ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BG])]
    table.setStyle(TableStyle(style))
    return table


def _gates_table(gates):
    rows = [[Paragraph("STUFE", _style("gh0", 7.5, 10, colors.white, bold=True)), Paragraph("PRÜFUNG", _style("gh1", 7.5, 10, colors.white, bold=True)),
             Paragraph("WERT", _style("gh2", 7.5, 10, colors.white, bold=True)), Paragraph("SCHWELLE (CONFIG)", _style("gh3", 7.5, 10, colors.white, bold=True))]]
    for g in gates:
        tone = HEX[g.get("status", "neutral")]
        rows.append([Paragraph(f'<font color="{tone}">●</font>&nbsp;<b>{g["stage"]}</b>', _style("gs", 8.5, 11.5)),
                     Paragraph(g["name"], _style("gn", 8.5, 11.5)),
                     Paragraph(f'<b>{g["value"]}</b>', _style("gv", 9, 11.5)),
                     Paragraph(g["rule"], _style("gr", 8, 11, MUTED))])
    table = Table(rows, colWidths=[28 * mm, 52 * mm, 28 * mm, 66 * mm])
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                               ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BG]),
                               ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
                               ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6)]))
    return table


def _dec(x, digits=1):
    return f"{x:.{digits}f}".replace(".", ",")


def _usd(x):
    return _dec(x, 2) + " $"


def _pct(x):
    return f"{x * 100:.1f} %".replace(".", ",")


def _num(x):
    return f"{x:.1f}".replace(".", ",")


def _fit_size(text, max_size, width_pt, font="DV-B"):
    size = max_size
    while size > 9 and pdfmetrics.stringWidth(text, font, size) > width_pt:
        size -= 0.5
    return size


def _kpi_cards(kpis):
    gap, n = 3, len(kpis)
    tile = (CONTENT_W - gap * (n - 1)) / n
    inner_pt = (tile - 7) * mm
    cells, widths = [], []
    for i, k in enumerate(kpis):
        tone = TONES[k["status"]]
        size = _fit_size(k["main"], 20, inner_pt)
        rows = [[Paragraph(k["label"].upper(), S_KPI_L)],
                [Paragraph(k["main"], _style("kv%d" % i, size, size + 4, tone, bold=True))],
                [Paragraph(k["sub"], S_KPI_S)]]
        if k.get("note"):
            rows.append([Paragraph(k["note"], _style("kn%d" % i, 7.2, 9.5, tone))])
        inner = Table(rows, colWidths=[(tile - 7) * mm])
        inner.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                   ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        cells.append(inner)
        widths.append(tile * mm)
        if i < n - 1:
            cells.append("")
            widths.append(gap * mm)
    table = Table([cells], colWidths=widths)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
             ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]
    for i, k in enumerate(kpis):
        col = 2 * i
        style += [("BACKGROUND", (col, 0), (col, 0), BG), ("LINEABOVE", (col, 0), (col, 0), 3, TONES[k["status"]]),
                  ("LEFTPADDING", (col, 0), (col, 0), 3.5 * mm), ("RIGHTPADDING", (col, 0), (col, 0), 3.5 * mm)]
    table.setStyle(TableStyle(style))
    return table


def _bullets(items, tone):
    return [Paragraph(f'<font color="{HEX[tone]}">●</font>&nbsp;&nbsp;{t}', _style("b" + tone, 9, 13, leftIndent=12, firstLineIndent=-12, spaceAfter=4))
            for t in items]


def _timeline(events):
    rows = []
    for e in events:
        future = e.get("future")
        color = HEX["neutral"] if future else "#14213d"
        dot = "○" if future else "●"
        rows.append([Paragraph(f'<font color="{color}"><b>{e["date"]}</b></font>', _style("td", 9, 12.5)),
                     Paragraph(f'<font color="{color}">{dot}</font>', _style("tdot", 9, 12.5)),
                     Paragraph(e["text"] + (" <font color='#667385'>(offen)</font>" if future else ""), _style("tt", 9, 12.5))])
    table = Table(rows, colWidths=[24 * mm, 8 * mm, 142 * mm])
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3.5),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5), ("LEFTPADDING", (0, 0), (-1, -1), 2),
                               ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE), ("LINEAFTER", (0, 0), (0, -1), 0.4, LINE)]))
    return table


def _entry_box(entry):
    lines = [
        Paragraph(entry["zone_title"], _style("et", 11, 15, NAVY, bold=True)),
        Spacer(1, 3),
        Paragraph(f'Zeithorizont: <b>{entry["horizon"]}</b>', S_CARD),
        Paragraph((f'{entry["limit_label"]}: <b>{_usd(entry["limit"])}</b> (Kurs {_usd(entry["price"])} am {entry["price_date"]}, '
                   f'{entry["price_vs_limit"]}). ' if entry.get("limit") else
                   f'{entry["limit_label"]}: <b>nicht berechenbar</b> (Kurs {_usd(entry["price"])} am {entry["price_date"]}). ') + entry["limit_note"], S_CARD),
    ]
    if entry.get("offer_value"):  # nur bei einem laufenden Angebot (zum Beispiel PPC)
        lines.append(Paragraph(f'{entry.get("offer_label", "Angebotswert")}: {_usd(entry["offer_value"])} am {entry["offer_date"]}.', S_CARD))
    lines.append(Paragraph("<b>Bedingung:</b> " + entry["condition"], S_CARD))
    return _card(lines, "neutral")


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.5)
    canvas.line(18 * mm, 14 * mm, 192 * mm, 14 * mm)
    canvas.setFont("DV", 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9.5 * mm, "Entwurf des Agenten. Keine Anlageberatung, keine Prognose, kein Kursziel. Entscheidung: Konstantin.")
    canvas.drawRightString(192 * mm, 9.5 * mm, f"Seite {doc.page}")
    canvas.restoreState()


def _header(c, v, data):
    d = data["dates"]
    left = [Paragraph(f'{c["name"]} <font size="12" color="#9fb3d1">{c["ticker"]}</font>', S_TITLE), Spacer(1, 4),
            Paragraph(f'Kandidat Nr. {c["list_nr"]} · Rang {c["stage2_rank"]} von {c["stage2_rank_of"]} in Stufe 2 · Vergleichsgruppe: {c["peer_group"]}', S_SUB),
            Paragraph(f'Unterlagen bis {d["documents_until"]} · Lauf {d["archive_run"]} · One-Pager vom {d["one_pager"]}', S_SUB),
            Paragraph("Kurs: " + d["price"], S_SUB)]
    badge = [Paragraph("VORSCHLAG DES AGENTEN", _style("bl", 7, 9, colors.white)), Spacer(1, 3),
             Paragraph(v["label"], _style("vl", 10, 13, colors.white, bold=True))]
    table = Table([[left, badge]], colWidths=[124 * mm, 50 * mm])
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), NAVY), ("BACKGROUND", (1, 0), (1, 0), TONES[v["tone"]]),
                               ("VALIGN", (0, 0), (0, 0), "TOP"), ("VALIGN", (1, 0), (1, 0), "MIDDLE"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                               ("TOPPADDING", (0, 0), (-1, -1), 14), ("BOTTOMPADDING", (0, 0), (-1, -1), 14)]))
    return table


def _images_row(items, widths=(84, 84), gap=6):
    """Zwei Bilder nebeneinander mit Abstand; items sind (Puffer, max. Hoehe in mm)."""
    cells = []
    for buf, height in items:
        cells.append(Image(buf, width=84 * mm, height=height * mm, kind="proportional") if buf else "")
    while len(cells) < 2:
        cells.append("")
    table = Table([[cells[0], "", cells[1]]], colWidths=[84 * mm, gap * mm, 84 * mm])
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    return table


def build(data, out_path, peers=None):
    c = data["company"]
    v = data["verdict"]
    story = [_header(c, v, data), Spacer(1, 10)]
    verdict_flow = [Paragraph(v["text"], _style("vt", 11, 16, TONES[v["tone"]], bold=True))]
    if v.get("legend"):
        verdict_flow += [Spacer(1, 5), Paragraph(v["legend"], _style("vg", 8.5, 12.5, MUTED))]
    story += [_card(verdict_flow, v["tone"]), Spacer(1, 12), _kpi_cards(data["kpis"]),
              Paragraph("Kernaussage", S_H), Paragraph(data["thesis"], _style("th", 9.6, 14)), Spacer(1, 10),
              _entry_box(data["entry"])]

    if data.get("funnel"):
        story.append(CondPageBreak(190 * mm))
        story.append(Paragraph(data.get("funnel_title", "Weg durch den Trichter"), S_H))
        story.append(Image(funnel_chart(data["funnel"]), width=CONTENT_W * mm, height=78 * mm, kind="proportional"))
        if data.get("funnel_note"):
            story.append(Paragraph(data["funnel_note"], S_SMALL))
        if data.get("gates"):
            story += [Paragraph("Die Prüfungen im Detail", S_H2), _gates_table(data["gates"])]
        if data.get("gates_note"):
            story += [Spacer(1, 3), Paragraph(data["gates_note"], S_SMALL)]
    if data.get("financials"):
        story.append(Paragraph(data.get("financials_title", "Kennzahlen"), S_H))
        story.append(_fin_table(data["financials"]))
        story.append(Spacer(1, 3))
        story.append(Paragraph(data["financials"].get("note", ""), S_SMALL))

    ticker = c["ticker"]
    story += [CondPageBreak(215 * mm), Paragraph("Einordnung gegen die Konkurrenz", S_H)]
    peer_notes, items_top, items_bottom = [], [], []
    if data.get("halfyear"):
        items_top.append((halfyear_chart(data), 62))
    if peers:
        png, note = valuation_map(peers, ticker)
        peer_notes.append(note)
        if png:
            items_top.append((png, 66))
        for key, title, fmt, lower in (("ev_ebitda", "EV/EBITDA", _num, True), ("fcf_yield", "FCF-Rendite", _pct, False)):
            png, note = peer_chart(peers, ticker, key, title, fmt, lower)
            peer_notes.append(note)
            if png:
                items_bottom.append((png, 84))
    if not peers:
        story.append(Paragraph("Vergleich mit der Konkurrenz: Peer-Daten fehlen (Export mit run_export_peers.py). "
                               "Ohne sie steht hier kein Diagramm; die Einordnung gegen die Peer-Gruppe ist offen.", S_SMALL))
    else:
        if data.get("peer_summary"):
            story += [_card([Paragraph(data["peer_summary"], _style("ps", 10, 14.5, NAVY, bold=True))], "neutral"), Spacer(1, 6)]
    if items_top:
        story.append(_images_row(items_top[:2]))
    if items_bottom:
        story.append(_images_row(items_bottom[:2]))
    if peers:
        own = next((p for p in peers if p["ticker"] == ticker), None)
        rank_line = ""
        if own and own.get("peer_rank") and own.get("peer_n"):
            rank_line = (f"Position in der Peer-Gruppe (EV/EBITDA, Stufe 2): Platz {int(float(own['peer_rank']))} von "
                         f"{int(float(own['peer_n']))}")
            if own.get("ev_ebitda_percentile") is not None:
                rank_line += f", {_dec(own['ev_ebitda_percentile'] * 100, 0)} % der Gruppe sind günstiger"
            rank_line += ". "
        shown, _ = shown_rows(peers, ticker, "ev_ebitda", True)
        named = [f'{p["ticker"]} {p["name"]}' for p in shown if p.get("name")]
        if named:
            story.append(Paragraph("<b>Vergleichsfirmen (" + ("alle" if len(shown) == len(peers) else "Ausschnitt") + "):</b> " +
                                   " · ".join(named), S_SMALL))
            story.append(Spacer(1, 3))
        extra = (data.get("peer_note", "") + " ") if data.get("peer_note") else ""
        story.append(Paragraph(rank_line + " ".join(dict.fromkeys(peer_notes)) + " " + extra + "Die Diagramme zeigen die Archivwerte der "
                               "Stufe 2 (Geschäftsjahre bzw. 3-Jahres-Mittel); die Karten auf Seite 1 zeigen teils die letzten zwölf Monate.", S_SMALL))

    story += [CondPageBreak(150 * mm), Paragraph("Was ist passiert", S_H), _timeline(data["events"]), Spacer(1, 14)]
    cols = Table([[[Paragraph("Risiken (Bear)", _style("bh", 11, 14, TONES["bad"], bold=True)), Spacer(1, 4)] + _bullets(data["bear"], "bad"),
                   "",
                   [Paragraph("Argumente dafür (Bull)", _style("gh", 11, 14, TONES["ok"], bold=True)), Spacer(1, 4)] + _bullets(data["bull"], "ok")]],
                 colWidths=[84 * mm, 6 * mm, 84 * mm])
    cols.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (0, 0), colors.HexColor(TINT["bad"])),
                              ("BACKGROUND", (2, 0), (2, 0), colors.HexColor(TINT["ok"])), ("LEFTPADDING", (0, 0), (-1, -1), 10),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 10),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("LEFTPADDING", (1, 0), (1, 0), 0), ("RIGHTPADDING", (1, 0), (1, 0), 0)]))
    story += [cols, Spacer(1, 14),
              _card([Paragraph("Ausstieg (Kill-Kriterium)", _style("kh", 11, 15, TONES["bad"], bold=True)), Spacer(1, 3),
                     Paragraph(data["kill"], S_BODY)], "bad")]

    story += [PageBreak(), Paragraph("Anhang: Quellen und Grenzen", S_H), Paragraph("Quellen", S_H2),
              Paragraph(data["sources"], _style("sr", 8.5, 12.5, MUTED))]
    for line in data.get("sources_detail", []):
        story.append(Paragraph("• " + line, _style("sd", 8.5, 12.5, MUTED, leftIndent=8, firstLineIndent=-8, spaceBefore=2)))
    story += [Paragraph("Nicht geprüft", S_H2), Paragraph(data["not_checked"], _style("nc", 8.5, 12.5, MUTED))]
    if data.get("method"):
        story += [Paragraph("Methode", S_H2), Paragraph(data["method"], _style("me", 8.5, 12.5, MUTED))]

    doc = SimpleDocTemplate(str(out_path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=20 * mm, title=f'One-Pager {c["ticker"]}', author="Equity-Research (Agent)")
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

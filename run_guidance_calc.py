"""Rechnet die Stufe-3-Kennzahlen auf Guidance-Basis aus Zahlen, die ein Mensch aus den Primaerquellen eintraegt.

Roadmap D.2, docs/stufe3/checkliste.md Punkte 3 und 4. Kein Abruf, keine Datenbank, kein SimFin: es werden nur die
Zahlen aus den Argumenten verrechnet. Alle Betraege in derselben Einheit (zum Beispiel Millionen Dollar).

Beispiel:
    python run_guidance_calc.py --market-cap 1000 --fcf-guidance 120 --net-debt 200 --ebitda-guidance 150 \\
        --net-debt-quarter 300 --ebitda-ttm 200 --table-fcf-yield 0.10

Exit-Code: 0 = alle drei Kennzahlen berechnet; 1 = mindestens eine nicht berechenbar (unzureichende Evidenz oder
unbrauchbare Eingabe); 2 = falsche Argumente.
"""

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from config_layer.thresholds import load_thresholds  # noqa: E402
from metrics_layer import guidance  # noqa: E402


def _de(value, decimals=2):
    return f"{value:.{decimals}f}".replace(".", ",")


def _g(value):
    return f"{value:g}"


def _reason_text(result, missing_hint):
    if result.invalid_reason == guidance.INSUFFICIENT_EVIDENCE:
        return f"unzureichende Evidenz (fehlt oder unbrauchbar: {missing_hint})"
    if result.invalid_reason == guidance.MARKET_CAP_UNUSABLE:
        return "nicht berechenbar (Marktkapitalisierung nicht verwertbar, muss größer als 0 sein)"
    if result.invalid_reason == guidance.EBITDA_NON_POSITIVE:
        return "nicht berechenbar (EBITDA nicht positiv)"
    return f"nicht berechenbar ({result.invalid_reason})"


def _compare(label_unit, value, table, percent):
    if table is None:
        return ""
    delta = guidance.difference(value, table)
    if percent:
        shown = f"Tabellenwert {_de(table * 100)} %"
        gap = "" if delta is None else f", Abstand {delta * 100:+.2f} Prozentpunkte".replace(".", ",")
    else:
        shown = f"Tabellenwert {_de(table)}"
        gap = "" if delta is None else f", Abstand {delta:+.2f}".replace(".", ",")
    return f" · {shown}{gap}"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Stufe-3-Kennzahlen auf Guidance-Basis (nur Rechnung, kein Abruf)")
    for name in ("market-cap", "fcf-guidance", "net-debt", "ebitda-guidance", "net-debt-quarter", "ebitda-ttm",
                 "table-fcf-yield", "table-ev-ebitda", "table-net-debt-ebitda"):
        parser.add_argument(f"--{name}", type=float, default=None)
    args = parser.parse_args(argv)

    thresholds = load_thresholds().net_debt_ebitda
    ok = True

    fcf = guidance.fcf_yield_guidance(args.fcf_guidance, args.market_cap)
    if fcf.value is None:
        ok = False
        print(f"FCF-Rendite (Guidance): {_reason_text(fcf, '--fcf-guidance, --market-cap')}")
    else:
        print(
            f"FCF-Rendite (Guidance): {_de(fcf.value * 100)} %  [{_g(args.fcf_guidance)} / {_g(args.market_cap)}]"
            + _compare("", fcf.value, args.table_fcf_yield, percent=True)
        )

    ev = guidance.ev_ebitda_guidance(args.market_cap, args.net_debt, args.ebitda_guidance)
    if ev.value is None:
        ok = False
        print(f"EV/EBITDA (Guidance): {_reason_text(ev, '--market-cap, --net-debt, --ebitda-guidance')}")
    else:
        print(
            f"EV/EBITDA (Guidance): {_de(ev.value)}  [({_g(args.market_cap)} + {_g(args.net_debt)}) / {_g(args.ebitda_guidance)}]"
            + _compare("", ev.value, args.table_ev_ebitda, percent=False)
        )

    lev = guidance.net_debt_ebitda_latest_quarter(args.net_debt_quarter, args.ebitda_ttm)
    if lev.value is None:
        ok = False
        print(f"Net Debt/EBITDA (letztes Quartal): {_reason_text(lev, '--net-debt-quarter, --ebitda-ttm')}")
    else:
        kind = guidance.classify_leverage(lev.value, thresholds)
        text = {
            guidance.CLEAN_PASS: f"sauber bestanden (unter {_de(thresholds.clean_pass_below, 1)})",
            guidance.GREY_ZONE: f"Grauzone ({_de(thresholds.clean_pass_below, 1)} bis unter {_de(thresholds.fail_at_or_above, 1)})",
            guidance.FAIL: f"durchgefallen (ab {_de(thresholds.fail_at_or_above, 1)})",
        }[kind]
        print(
            f"Net Debt/EBITDA (letztes Quartal): {_de(lev.value)}  [{_g(args.net_debt_quarter)} / {_g(args.ebitda_ttm)}]"
            f" – {text}" + _compare("", lev.value, args.table_net_debt_ebitda, percent=False)
        )

    print("Hinweis: nur Rechnung aus den eingegebenen Zahlen; Quelle und Datum jeder Zahl gehören in den One-Pager.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

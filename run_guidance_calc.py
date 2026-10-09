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

from config_layer.stage2 import load_stage2_config  # noqa: E402
from config_layer.thresholds import load_thresholds  # noqa: E402
from metrics_layer import guidance  # noqa: E402


def _decimal_point(text):
    """Argument als Zahl mit Dezimalpunkt; ein Komma wird mit klarer Meldung abgelehnt (1,5 oder 1,500 waeren mehrdeutig)."""
    if "," in text:
        raise argparse.ArgumentTypeError(f"{text!r}: bitte den Dezimalpunkt verwenden (zum Beispiel 1.5) und kein Tausendertrennzeichen")
    try:
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} ist keine Zahl") from None


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
    if result.invalid_reason == guidance.EV_NON_POSITIVE:
        return "nicht berechenbar (Enterprise Value nicht positiv: Marktkapitalisierung plus Nettoverschuldung ist 0 oder kleiner)"
    if result.invalid_reason in (guidance.EV_EBITDA_IMPLAUSIBLY_HIGH, guidance.FCF_YIELD_IMPLAUSIBLY_HIGH):
        return "unplausibel hoch (Einheitenfehler oder EBITDA/FCF fast 0? Grenze aus config/stage2.yaml); bitte Eingaben prüfen"
    return f"nicht berechenbar ({result.invalid_reason})"


def _compare(value, table, percent):
    """Zusatz " · Tabellenwert ..., Abstand ..." (Abstand = Guidance minus Tabellenwert); leer ohne Tabellenwert."""
    if table is None:
        return ""
    if guidance._number(table) is None:
        return " · Tabellenwert unbrauchbar (keine endliche Zahl)"
    delta = guidance.difference(value, table)
    if percent:
        shown = f"Tabellenwert {_de(table * 100)} %"
        gap = f", Abstand {delta * 100:+.2f} Prozentpunkte (Guidance minus Tabellenwert)".replace(".", ",")
    else:
        shown = f"Tabellenwert {_de(table)}"
        gap = f", Abstand {delta:+.2f} (Guidance minus Tabellenwert)".replace(".", ",")
    return f" · {shown}{gap}"


def _signed(value):
    return f"({_g(value)})" if value < 0 else _g(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Stufe-3-Kennzahlen auf Guidance-Basis (nur Rechnung, kein Abruf)")
    for name in ("market-cap", "fcf-guidance", "net-debt", "ebitda-guidance", "net-debt-quarter", "ebitda-ttm",
                 "table-fcf-yield", "table-ev-ebitda", "table-net-debt-ebitda", "shares", "price", "peer-multiple"):
        parser.add_argument(
            f"--{name}",
            type=_decimal_point,
            default=None,
            help=("Anteil, nicht Prozent (0.10 = 10 %%)" if name == "table-fcf-yield" else "Zahl mit Dezimalpunkt"),
        )
    args = parser.parse_args(argv)

    thresholds = load_thresholds().net_debt_ebitda
    stage2 = load_stage2_config()
    ok = True

    fcf = guidance.fcf_yield_guidance(args.fcf_guidance, args.market_cap, implausible_above=stage2.fcf_yield.implausible_above)
    if fcf.value is None:
        ok = False
        print(f"FCF-Rendite (Guidance): {_reason_text(fcf, '--fcf-guidance, --market-cap')}")
    else:
        print(
            f"FCF-Rendite (Guidance): {_de(fcf.value * 100)} %  [{_g(args.fcf_guidance)} / {_g(args.market_cap)}]"
            + _compare(fcf.value, args.table_fcf_yield, percent=True)
        )

    ev = guidance.ev_ebitda_guidance(
        args.market_cap, args.net_debt, args.ebitda_guidance, implausible_above=stage2.ev_ebitda.implausible_above
    )
    if ev.value is None:
        ok = False
        print(f"EV/EBITDA (Guidance): {_reason_text(ev, '--market-cap, --net-debt, --ebitda-guidance')}")
    else:
        print(
            f"EV/EBITDA (Guidance): {_de(ev.value)}  [({_g(args.market_cap)} + {_signed(args.net_debt)}) / {_g(args.ebitda_guidance)}]"
            + _compare(ev.value, args.table_ev_ebitda, percent=False)
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
        distance = f"; Abstand zur Durchfall-Schwelle {_de(thresholds.fail_at_or_above - lev.value)}"
        print(
            f"Net Debt/EBITDA (letztes Quartal): {_de(lev.value)}  [{_signed(args.net_debt_quarter)} / {_g(args.ebitda_ttm)}]"
            f" – {text}{distance}" + _compare(lev.value, args.table_net_debt_ebitda, percent=False)
        )

    if args.price is not None and args.shares is None:
        ok = False
        print("\nEinstiegsgrenze: nicht gerechnet, weil --price ohne --shares angegeben wurde (die Aktienzahl fehlt).")
    if args.shares is not None:
        target_yield = stage2.fcf_yield.clean_pass_above
        limits = [
            ("FCF-Rendite", guidance.price_limit_fcf_yield(args.fcf_guidance, args.shares, target_yield)),
            (
                "EV/EBITDA",
                guidance.price_limit_ev_ebitda(
                    args.ebitda_guidance, args.net_debt, args.shares, args.peer_multiple,
                    implausible_above=stage2.ev_ebitda.implausible_above,
                ),
            ),
        ]
        print("\nEinstiegsgrenze (Preis je Aktie an der Kante der eigenen Bewertungsregeln, gerechnet mit den eingetragenen Zahlen; kein Kursziel, keine Prognose):")
        print("  Basis der Zahlen: so wie eingetragen. Stammen FCF und EBITDA aus der Guidance, ist die Grenze eine Rechnung auf Basis einer Managementprognose (nicht geprüft).")
        for name, result in limits:
            if result.value is None:
                ok = False
                reason = (
                    "Zielvielfaches unplausibel hoch (Einheitenfehler? Grenze aus config/stage2.yaml)"
                    if result.invalid_reason == guidance.EV_EBITDA_IMPLAUSIBLY_HIGH
                    else f"unzureichende Evidenz oder nicht berechenbar ({result.invalid_reason})"
                )
                print(f"  über {name}: {reason}")
                continue
            if name == "FCF-Rendite":
                basis = (
                    f"Zielrendite {_de(target_yield * 100, 1)} % (config/stage2.yaml, fcf_yield.clean_pass_above; "
                    "die Regel verlangt strikt darüber, der Preis muss also etwas unter der Kante liegen)"
                )
            else:
                basis = f"Zielvielfaches {_g(args.peer_multiple)} (von Hand eingetragene Drittelgrenze der Peer-Gruppe; Peer-Werte beruhen auf Ist-EBITDA, das hier eingetragene EBITDA kann ein anderer Zeitbezug sein)"
            print(f"  über {name}: {_de(result.value)}  [{basis}]")
        limit = guidance.entry_limit(limits)
        if limit.price is None:
            print("  Ergebnis: keine Grenze berechenbar (unzureichende Evidenz).")
        else:
            warning = ""
            if limit.missing:
                warning = f"; ACHTUNG: {', '.join(limit.missing)} nicht berechenbar, die strengere Grenze fehlt, die Grenze kann zu hoch sein"
            print(f"  Ergebnis: Kante {_de(limit.price)} (bindend = die strengere Grenze: {limit.binding}){warning}")
            gap = guidance.distance_to_limit(limit.price, args.price)
            if gap is not None:
                where = "über" if gap > 0 else "unter"
                print(f"  Kurs {_de(args.price)} liegt {_de(abs(gap) * 100, 1)} % {where} der Grenze (kein Kauf- oder Verkaufssignal)")
            elif args.price is not None:
                ok = False
                print("  Kurs unbrauchbar (muss größer als 0 sein)")
        if args.price is not None and args.market_cap is not None and guidance._number(args.price) and guidance._number(args.market_cap):
            print(
                f"  Gegenprobe: Kurs × Aktienzahl = {_g(args.price * args.shares)}, eingetragene Marktkapitalisierung = {_g(args.market_cap)} "
                "(sollten ähnlich sein; sonst stimmt die Aktienzahl oder der Kurs nicht)"
            )
        print("  Hinweise: Der Enterprise Value enthält keine Minderheitsanteile und Vorzugsaktien; die Aktienzahl soll verwässert und über alle Aktiengattungen gerechnet sein.")
        print("  Die Grenze wird nicht gespeichert und ist keine Kauforder; sie wird mit jedem neuen Quartal oder jeder neuen Guidance neu gerechnet.")
    print("Hinweis: nur Rechnung aus den eingegebenen Zahlen; Quelle und Datum jeder Zahl gehören in den One-Pager.")
    print("Hinweis: Guidance-Werte und das EBITDA der letzten zwölf Monate haben einen anderen Zeitbezug als die Tabellenwerte (Dreijahresschnitt bzw. Geschäftsjahr); der Abstand ist ein Vergleich, kein Fehler.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

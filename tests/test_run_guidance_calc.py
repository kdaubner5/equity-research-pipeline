"""run_guidance_calc.py: rechnet die Stufe-3-Kennzahlen aus von Hand eingegebenen Zahlen (Roadmap D.2)."""

import pytest

import run_guidance_calc as run


def test_all_inputs_print_values_formulas_and_leverage_class(capsys):
    code = run.main([
        "--market-cap", "1000", "--fcf-guidance", "120", "--net-debt", "200", "--ebitda-guidance", "150",
        "--net-debt-quarter", "300", "--ebitda-ttm", "200",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "FCF-Rendite (Guidance): 12,00 %" in out and "120 / 1000" in out
    assert "EV/EBITDA (Guidance): 8,00" in out and "(1000 + 200) / 150" in out
    assert "Net Debt/EBITDA (letztes Quartal): 1,50" in out and "sauber bestanden" in out


def test_missing_inputs_print_insufficient_evidence_not_a_number(capsys):
    code = run.main(["--market-cap", "1000"])
    out = capsys.readouterr().out
    assert code == 1 and out.count("unzureichende Evidenz") == 3


def test_table_values_are_compared(capsys):
    run.main(["--market-cap", "1000", "--fcf-guidance", "120", "--table-fcf-yield", "0.10"])
    out = capsys.readouterr().out
    assert "Tabellenwert 10,00 %" in out and "Abstand" in out and "+2,00" in out


def test_non_numeric_argument_is_a_clear_error_exit_2(capsys):
    with pytest.raises(SystemExit) as info:
        run.main(["--market-cap", "abc"])
    assert info.value.code == 2


def test_negative_market_cap_is_reported_not_computed(capsys):
    code = run.main(["--market-cap", "-5", "--fcf-guidance", "10"])
    assert code == 1 and "Marktkapitalisierung" in capsys.readouterr().out


def test_leverage_is_classified_against_the_real_stage1_thresholds(capsys):
    run.main(["--net-debt-quarter", "480", "--ebitda-ttm", "200"])  # 2,4: Grauzone
    assert "Grauzone" in capsys.readouterr().out
    run.main(["--net-debt-quarter", "500", "--ebitda-ttm", "200"])  # 2,5: durchgefallen
    assert "durchgefallen" in capsys.readouterr().out

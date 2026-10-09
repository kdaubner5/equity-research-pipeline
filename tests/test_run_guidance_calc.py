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


def test_net_cash_case_prints_a_signed_formula_and_clean_pass(capsys):
    run.main(["--market-cap", "1000", "--net-debt", "-200", "--ebitda-guidance", "100",
              "--net-debt-quarter", "-100", "--ebitda-ttm", "200"])
    out = capsys.readouterr().out
    assert "(1000 + (-200)) / 100" in out and "8,00" in out
    assert "-0,50" in out and "sauber bestanden" in out


def test_negative_enterprise_value_is_explained(capsys):
    run.main(["--market-cap", "1000", "--net-debt", "-1200", "--ebitda-guidance", "100"])
    assert "Enterprise Value nicht positiv" in capsys.readouterr().out


def test_implausible_values_from_a_unit_mistake_are_flagged(capsys):
    run.main(["--market-cap", "1000", "--net-debt", "0", "--ebitda-guidance", "0.0001"])
    assert "unplausibel hoch" in capsys.readouterr().out


def test_decimal_comma_is_refused_with_a_clear_message(capsys):
    with pytest.raises(SystemExit) as info:
        run.main(["--market-cap", "1,5"])
    assert info.value.code == 2
    assert "Dezimalpunkt" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_table_values_that_are_not_finite_are_reported_as_unusable(capsys, bad):
    run.main(["--market-cap", "1000", "--fcf-guidance", "100", f"--table-fcf-yield={bad}"])
    out = capsys.readouterr().out
    assert "Tabellenwert unbrauchbar" in out and "nan %" not in out and "inf %" not in out


def test_distance_to_the_fail_threshold_and_direction_of_the_gap_are_printed(capsys):
    run.main(["--net-debt-quarter", "400", "--ebitda-ttm", "200", "--table-net-debt-ebitda", "1.5"])
    out = capsys.readouterr().out
    assert "Abstand zur Durchfall-Schwelle 0,50" in out and "Guidance minus Tabellenwert" in out
    assert "+0,50" in out


def test_the_help_text_says_that_the_table_fcf_yield_is_a_share_not_a_percentage(capsys):
    with pytest.raises(SystemExit):
        run.main(["--help"])
    assert "Anteil, nicht Prozent" in capsys.readouterr().out


def test_the_runner_reads_only_config_and_does_no_network_or_database_access():
    source = open(run.__file__, encoding="utf-8").read()
    for forbidden in ("urllib", "requests", "sqlite3", "simfin", "socket"):
        assert forbidden not in source.lower().replace("kein simfin", "")


def test_entry_limit_block_is_printed_with_both_limits_the_binding_one_and_the_distance(capsys):
    code = run.main(["--market-cap", "1000", "--fcf-guidance", "120", "--net-debt", "200", "--ebitda-guidance", "150",
                     "--shares", "100", "--price", "12", "--peer-multiple", "8",
                     "--net-debt-quarter", "300", "--ebitda-ttm", "200"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Einstiegsgrenze" in out and "kein Kursziel" in out
    assert "20,00" in out and "10,00" in out  # FCF-Rendite 6 %: 20 je Aktie; EV/EBITDA 8: 10 je Aktie
    assert "bindend: EV/EBITDA" in out
    assert "Kurs 12,00 liegt 20,0 % über der Grenze" in out


def test_entry_limit_uses_the_stage2_clean_pass_yield_and_says_so(capsys):
    run.main(["--fcf-guidance", "120", "--shares", "100"])
    out = capsys.readouterr().out
    assert "6,0 %" in out and "stage2.yaml" in out


def test_entry_limit_with_missing_inputs_says_insufficient_evidence_and_gives_no_price(capsys):
    code = run.main(["--shares", "100"])
    out = capsys.readouterr().out
    assert code == 1 and "Einstiegsgrenze" in out and "unzureichende Evidenz" in out


def test_without_shares_no_entry_limit_block_is_printed(capsys):
    run.main(["--market-cap", "1000", "--fcf-guidance", "120"])
    assert "Einstiegsgrenze" not in capsys.readouterr().out


def test_price_below_the_limit_is_described_as_room(capsys):
    run.main(["--fcf-guidance", "120", "--shares", "100", "--price", "16"])
    assert "unter der Grenze" in capsys.readouterr().out

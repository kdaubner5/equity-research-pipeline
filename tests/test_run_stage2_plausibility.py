"""run_stage2.py with the two plausibility checks (docs/decisions.md "Stufe 2 -
Datenplausibilitaet: Aktienzahl und Investitionen", Abschnitt 3 und 5, Test 5). Offline, tmp_path
only; the 23 pilot fixtures serve as the fundamentals cache, the price cache is built by hand."""

import io
import json

import pytest

from universe_fakes import PRICE_COLUMNS
from test_run_stage2 import ALL_PILOTS, World, _set_cell, raw, seed_cache

import run_stage2


def price_record(company_id, *, shares, dividend, ticker=None):
    rows = [["2026-09-22", None, shares, 10.0, 10.0, 10.0, 10.0, 10.0, 100], ["2026-09-23", dividend, shares, 10.0, 10.0, 10.0, 10.0, 10.0, 100]]
    return {"id": company_id, "ticker": ticker or f"T{company_id}", "currency": "USD", "columns": PRICE_COLUMNS, "data": rows}


def write_prices(universe_root, records, *, name="batch.json", as_of="2026-09-24"):
    directory = universe_root / as_of / "prices"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps({"kind": "prices_batch", "response": records}), encoding="utf-8")
    return directory


def patch_cf(world, ticker, cells):
    """Overwrite whole cash-flow columns of one pilot fixture in the cache (all rows) with one value each."""
    record = raw(ticker)
    for column, value in cells.items():
        _set_cell(record, "CF", column, value)
    seed_cache(world.cache_dir, record["id"], record)


DIVIDENDS = "Dividends Paid"
OTHER = "Other Investing Activities"


def sid(ticker):
    return raw(ticker)["id"]


def result_rows(world, sql="SELECT ticker, status, flags, invalid_reasons, share_count_ratio, other_investing_triggered, fcf_yield_incl_other_investing, info_flags FROM stage2_results"):
    return {r[0]: r[1:] for r in world.stage2(sql)}


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path, survivors=("CENT", "SCSC", "KELYA"))
    w.universe_root = tmp_path / "universe"
    return w


def test_a_suspect_share_count_makes_the_survivor_not_evaluable_and_removes_it_from_the_peer_base(world):
    patch_cf(world, "CENT", {DIVIDENDS: -100.0, OTHER: 0.0})
    patch_cf(world, "SCSC", {DIVIDENDS: -100.0, OTHER: 0.0})
    write_prices(world.universe_root, [price_record(sid("CENT"), shares=5.0, dividend=1.0), price_record(sid("SCSC"), shares=25.0, dividend=1.0)])
    code, out = world.run(universe_cache_dir=world.universe_root)
    assert code == run_stage2.EXIT_OK, out
    rows = result_rows(world)
    assert rows["CENT"][:2] == ("not_evaluable", "") and rows["CENT"][2] == "share_count_suspect"
    assert rows["CENT"][3] == pytest.approx(20.0)  # R is archived
    assert rows["SCSC"][0] == "passed" and rows["SCSC"][3] == pytest.approx(4.0)
    assert world.stage2("SELECT peer_base_size FROM stage2_runs") == [(8,)]  # 9 valid multiples minus CENT
    assert world.stage2("SELECT ranked_count FROM stage2_runs") == [(1,)]


def test_the_report_counts_both_checks_with_their_reference(world):
    patch_cf(world, "CENT", {DIVIDENDS: -100.0, OTHER: 0.0})
    patch_cf(world, "SCSC", {DIVIDENDS: -100.0, OTHER: 0.0})
    write_prices(world.universe_root, [price_record(sid("CENT"), shares=5.0, dividend=1.0), price_record(sid("SCSC"), shares=25.0, dividend=1.0)])
    _, out = world.run(universe_cache_dir=world.universe_root)
    assert "aktienzahl_pruefung_vorfilter: von 23 Firmen geprueft=2 in_ordnung=1 mehrdeutig=0 verdaechtig=1 nicht_pruefbar=21" in out
    assert "aktienzahl_pruefung_ueberlebende: von 3 geprueft=2 in_ordnung=1 mehrdeutig=0 verdaechtig=1 nicht_pruefbar=1" in out
    assert "verdaechtige Ueberlebende: ['CENT']" in out
    assert "peer_basis_ohne_verdaechtige: ausgeschieden=1" in out
    assert "preis_cache: dateien=1 ungueltig=0 firmen_ungueltig=0" in out
    assert "investitionen_gegenrechnung: von 3 Ueberlebenden ausgeloest=0 flag_fcf_yield_unverified=0 ungeprueft=2" in out


def test_other_investing_flags_a_clean_survivor_and_the_report_counts_it(world):
    patch_cf(world, "SCSC", {OTHER: -5e9})  # far more than twice the capex line and larger than the whole market cap
    patch_cf(world, "CENT", {OTHER: 0.0})
    code, out = world.run(universe_cache_dir=None)
    assert code == 0, out
    row = result_rows(world)["SCSC"]
    assert row[0] == "passed_with_flags" and "fcf_yield_unverified" in row[1].split(",")
    assert row[4] == 1 and row[5] < 0  # triggered, counter-calculation negative
    assert "ausgeloest=1 flag_fcf_yield_unverified=1 ungeprueft=1" in out
    # the flagged company sorts after the unflagged survivors of the same Stage-1 flag count
    assert world.stage2("SELECT COUNT(*) FROM stage2_results WHERE flags LIKE '%fcf_yield_unverified%'") == [(1,)]


def test_without_a_price_cache_everything_is_unchecked_and_the_report_says_so(world):
    code, out = world.run()
    assert code == 0, out
    assert "preis_cache: nicht angegeben" in out
    assert "aktienzahl_pruefung_vorfilter: von 23 Firmen geprueft=0 in_ordnung=0 mehrdeutig=0 verdaechtig=0 nicht_pruefbar=23" in out
    rows = result_rows(world)
    assert all("share_count_unchecked" in r[6].split(",") for t, r in rows.items() if r[6])  # evaluable ones carry the mark
    assert world.stage2("SELECT peer_base_size FROM stage2_runs") == [(9,)]  # unchanged: nobody was removed


def test_the_unchecked_mark_is_set_for_companies_without_dividend_data(world):
    write_prices(world.universe_root, [price_record(sid("SCSC"), shares=25.0, dividend=None)])  # price entry but no dividend
    world.run(universe_cache_dir=world.universe_root)
    assert "share_count_unchecked" in result_rows(world)["SCSC"][6]


def test_a_broken_price_file_and_a_broken_company_record_are_counted_in_the_report(world):
    directory = write_prices(world.universe_root, [price_record(sid("SCSC"), shares=25.0, dividend=1.0), {"id": sid("CENT"), "columns": ["Date"], "data": [["x"]]}])
    (directory / "corrupt.json").write_text("{nope", encoding="utf-8")
    code, out = world.run(universe_cache_dir=world.universe_root)
    assert code == 0, out
    assert "preis_cache: dateien=1 ungueltig=1 firmen_ungueltig=1" in out
    assert "corrupt.json" in out


def test_a_missing_prices_directory_aborts_before_anything_is_written(world):
    code, _ = world.run(universe_cache_dir=world.universe_root)  # nothing created under it
    assert code == run_stage2.EXIT_TECHNICAL_FAILURE
    assert not world.archive_db.exists() and not world.csv_dir.exists()


def test_wrong_types_in_the_new_cash_flow_columns_isolate_the_company(world):
    patch_cf(world, "SCSC", {DIVIDENDS: "lots"})
    patch_cf(world, "CENT", {OTHER: "many"})
    code, out = world.run()
    assert code == run_stage2.EXIT_OK, out
    rows = result_rows(world)
    assert rows["SCSC"][:3] == ("not_evaluable", "", "fundamentals_malformed")
    assert rows["CENT"][:3] == ("not_evaluable", "", "fundamentals_malformed")


def test_a_company_without_the_new_columns_is_still_evaluated_and_counted_as_unavailable(world):
    record = raw("SCSC")
    cf = next(s for s in record["statements"] if s["statement"] == "CF")
    idx = cf["columns"].index("Other Investing Activities")
    cf["columns"].pop(idx)
    for row in cf["data"]:
        row.pop(idx)
    seed_cache(world.cache_dir, record["id"], record)
    code, out = world.run()
    assert code == 0, out
    row = result_rows(world)["SCSC"]
    assert row[0] == "passed" and row[4] is None  # evaluated, the counter-calculation is simply unchecked
    assert "other_investing_unavailable=1" in out


def test_the_verdict_is_reproducible_in_a_second_run_with_the_same_inputs(world):
    patch_cf(world, "CENT", {DIVIDENDS: -100.0})
    write_prices(world.universe_root, [price_record(sid("CENT"), shares=5.0, dividend=1.0)])
    first, _ = world.run(universe_cache_dir=world.universe_root)
    second, _ = world.run(universe_cache_dir=world.universe_root, run_timestamp="2026-10-07T13:00:00")
    assert (first, second) == (0, 0)
    ratios = [r[0] for r in world.stage2("SELECT share_count_ratio FROM stage2_results WHERE ticker = 'CENT' ORDER BY run_timestamp")]
    assert ratios == [pytest.approx(20.0), pytest.approx(20.0)]


def test_the_command_line_accepts_the_optional_universe_cache_dir_without_a_default():
    parser = run_stage2.build_parser()
    action = next(a for a in parser._actions if a.dest == "universe_cache_dir")
    assert action.required is False and action.default is None


def test_the_source_archive_is_still_not_modified(world):
    import hashlib

    before = hashlib.sha256(world.source_db.read_bytes()).hexdigest()
    write_prices(world.universe_root, [price_record(sid("CENT"), shares=5.0, dividend=1.0)])
    assert world.run(universe_cache_dir=world.universe_root)[0] == 0
    assert hashlib.sha256(world.source_db.read_bytes()).hexdigest() == before

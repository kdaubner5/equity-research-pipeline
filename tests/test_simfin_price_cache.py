"""Read-only index of the price cache (docs/decisions.md "Stufe 2 - Datenplausibilitaet: Aktienzahl
und Investitionen", Abschnitt 1 und 5, Test 4). Artificial envelopes in the format
simfin_universe.fetch_prices writes; no network, no real cache."""

import json
from datetime import date

import pytest

from data_layer.simfin_price_cache import PriceCacheError, load_price_cache_index
from universe_fakes import PRICE_COLUMNS


def row(day, *, dividend=None, shares=1_000_000, close=10.0, volume=100):
    return [day, dividend, shares, close, close, close, close, close, volume]


def record(company_id, rows, *, currency="USD", columns=None):
    return {"id": company_id, "ticker": f"T{company_id}", "currency": currency, "columns": columns or PRICE_COLUMNS, "data": rows}


def write(directory, name, records, *, kind="prices_batch"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        json.dumps({"kind": kind, "ids": [r["id"] for r in records], "start": "2026-06-11", "end": "2026-09-24", "response": records}),
        encoding="utf-8",
    )


def test_shares_and_the_last_dividend_come_from_the_latest_priced_rows(tmp_path):
    write(tmp_path, "a.json", [record(1, [
        row("2026-08-01", dividend=0.30, shares=900_000),
        row("2026-09-01", dividend=0.36, shares=1_000_000),
        row("2026-09-23", dividend=None, shares=1_100_000, close=12.0),
    ])])
    index = load_price_cache_index(tmp_path)
    entry = index.entries[1]
    assert entry.shares == 1_100_000  # the last row with a price, the same row the market cap uses
    assert entry.price_date == date(2026, 9, 23)
    assert entry.dividend_per_share == 0.36  # the last positive dividend in the window
    assert entry.dividend_date == date(2026, 9, 1)
    assert (index.files_read, index.files_invalid, index.companies_invalid) == (1, [], [])


def test_no_dividend_in_the_window_is_none_not_zero(tmp_path):
    write(tmp_path, "a.json", [record(1, [row("2026-09-23")])])
    entry = load_price_cache_index(tmp_path).entries[1]
    assert entry.dividend_per_share is None and entry.dividend_date is None


@pytest.mark.parametrize("dividend", [0.0, -0.5])
def test_a_zero_or_negative_dividend_is_not_a_dividend(tmp_path, dividend):
    write(tmp_path, "a.json", [record(1, [row("2026-09-22", dividend=0.4), row("2026-09-23", dividend=dividend)])])
    assert load_price_cache_index(tmp_path).entries[1].dividend_per_share == 0.4  # falls back to the earlier real one


def test_rows_without_price_or_shares_are_skipped_for_the_shares_value(tmp_path):
    write(tmp_path, "a.json", [record(1, [
        row("2026-09-22", shares=500_000),
        row("2026-09-23", shares=None),
        row("2026-09-24", close=None, shares=700_000),
    ])])
    entry = load_price_cache_index(tmp_path).entries[1]
    assert entry.shares == 500_000 and entry.price_date == date(2026, 9, 22)


def test_a_company_without_any_usable_row_is_reported_not_indexed(tmp_path):
    write(tmp_path, "a.json", [record(1, [row("2026-09-23", shares=None)]), record(2, [])])
    index = load_price_cache_index(tmp_path)
    assert index.entries == {}
    assert sorted(i for i, _ in index.companies_invalid) == [1, 2]


def test_batch_and_single_files_are_both_read(tmp_path):
    write(tmp_path, "batch.json", [record(1, [row("2026-09-23", dividend=0.1)])])
    write(tmp_path, "single_2_abc.json", [record(2, [row("2026-09-23", dividend=0.2)])], kind="prices_single")
    index = load_price_cache_index(tmp_path)
    assert set(index.entries) == {1, 2} and index.files_read == 2


def test_the_same_company_twice_keeps_the_later_price_date_and_counts_the_duplicate(tmp_path):
    write(tmp_path, "a.json", [record(1, [row("2026-09-20", dividend=0.1, shares=1)])])
    write(tmp_path, "b.json", [record(1, [row("2026-09-23", dividend=0.2, shares=2)])])
    index = load_price_cache_index(tmp_path)
    assert index.entries[1].shares == 2 and index.duplicate_ids == 1


def test_the_same_company_twice_with_the_same_date_is_deterministic(tmp_path):
    write(tmp_path, "b.json", [record(1, [row("2026-09-23", shares=2)])])
    write(tmp_path, "a.json", [record(1, [row("2026-09-23", shares=1)])])
    assert load_price_cache_index(tmp_path).entries[1].shares == 1  # file name order decides, not the OS listing order


def test_non_usd_records_are_not_indexed_and_counted(tmp_path):
    write(tmp_path, "a.json", [record(1, [row("2026-09-23")], currency="EUR")])
    index = load_price_cache_index(tmp_path)
    assert index.entries == {} and index.non_usd == 1


@pytest.mark.parametrize(
    "bad_record",
    [
        record(1, [["not-a-date", None, 1, 1, 1, 1, 1, 1, 1]]),
        record(1, [row("2026-09-23", shares="many")]),
        record(1, [row("2026-09-23", close=True)]),
        record(1, [row("2026-09-23")[:-1]]),  # row shorter than its columns
        record(1, [row("2026-09-23")], columns=["Date", "Common Shares Outstanding"]),  # required columns missing
        record(1, [row("2026-09-23", dividend="x")]),
    ],
)
def test_a_malformed_company_record_is_reported_and_does_not_stop_the_others(tmp_path, bad_record):
    write(tmp_path, "a.json", [bad_record, record(2, [row("2026-09-23", dividend=0.5)])])
    index = load_price_cache_index(tmp_path)
    assert 1 not in index.entries and 2 in index.entries
    assert [i for i, _ in index.companies_invalid] == [1]


def test_a_missing_dividend_column_is_a_company_without_dividend_not_an_error(tmp_path):
    columns = [c for c in PRICE_COLUMNS if c != "Dividend Paid"]
    data = [[d, s, c, a, h, l, o, v] for d, _, s, c, a, h, l, o, v in [row("2026-09-23")]]
    write(tmp_path, "a.json", [record(1, data, columns=columns)])
    entry = load_price_cache_index(tmp_path).entries[1]
    assert entry.dividend_per_share is None and entry.shares == 1_000_000


def test_broken_files_are_counted_with_their_name_and_never_ignored_silently(tmp_path):
    write(tmp_path, "good.json", [record(1, [row("2026-09-23")])])
    (tmp_path / "corrupt.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "list.json").write_text("[1, 2]", encoding="utf-8")
    (tmp_path / "kind.json").write_text(json.dumps({"kind": "other", "response": []}), encoding="utf-8")
    (tmp_path / "noresponse.json").write_text(json.dumps({"kind": "prices_batch", "response": {"a": 1}}), encoding="utf-8")
    (tmp_path / "bytes.json").write_bytes(b"\xff\xfe\x00bad")
    index = load_price_cache_index(tmp_path)
    assert set(index.entries) == {1}
    assert sorted(name for name, _ in index.files_invalid) == ["bytes.json", "corrupt.json", "kind.json", "list.json", "noresponse.json"]


def test_a_record_that_is_not_an_object_or_has_no_integer_id_is_reported(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"kind": "prices_batch", "response": [5, {"id": "x"}, {"id": True}, record(3, [row("2026-09-23")])]}), encoding="utf-8")
    index = load_price_cache_index(tmp_path)
    assert set(index.entries) == {3} and len(index.companies_invalid) == 3


def test_temporary_files_are_ignored(tmp_path):
    write(tmp_path, "a.json", [record(1, [row("2026-09-23")])])
    (tmp_path / "a.json.tmp").write_text("garbage", encoding="utf-8")
    index = load_price_cache_index(tmp_path)
    assert index.files_read == 1 and index.files_invalid == []


def test_a_missing_directory_is_an_error_not_an_empty_index(tmp_path):
    with pytest.raises(PriceCacheError, match="prices"):
        load_price_cache_index(tmp_path / "prices")


def test_an_empty_directory_gives_an_empty_index(tmp_path):
    index = load_price_cache_index(tmp_path)
    assert index.entries == {} and index.files_read == 0


def test_reading_does_not_modify_the_directory(tmp_path):
    write(tmp_path, "a.json", [record(1, [row("2026-09-23")])])
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    load_price_cache_index(tmp_path)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before

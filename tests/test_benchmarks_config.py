"""config/benchmarks.yaml (docs/decisions.md, "Messlatte - Kursreihen speichern und regelmaessig abrufen",
Punkt 4): jeder Schluessel Pflicht, keine Standardwerte im Code, Fehler nennen den Schluessel."""

import pytest

from config_layer.benchmarks import DEFAULT_BENCHMARKS_PATH, BenchmarksConfigError, load_benchmarks

VALID = """
source: alphavantage
pause_between_symbols_seconds: 13
stale_warn_days: 14
gap_calendar_days: 6
lookback_max_calendar_days: 5
jump_threshold_pct: 10
symbols:
  - {symbol: CSPX.LON, role: sp500, currency: USD}
  - {symbol: XDWD.DEX, role: msci_world, currency: EUR}
"""


def write(tmp_path, text):
    path = tmp_path / "benchmarks.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_file_loads(tmp_path):
    cfg = load_benchmarks(write(tmp_path, VALID))
    assert cfg.source == "alphavantage"
    assert cfg.pause_between_symbols_seconds == 13
    assert cfg.stale_warn_days == 14
    assert cfg.gap_calendar_days == 6
    assert cfg.lookback_max_calendar_days == 5
    assert cfg.jump_threshold_pct == 10
    assert [(s.symbol, s.role, s.currency) for s in cfg.symbols] == [
        ("CSPX.LON", "sp500", "USD"),
        ("XDWD.DEX", "msci_world", "EUR"),
    ]


def test_the_real_file_loads_and_has_the_two_decided_etfs():
    cfg = load_benchmarks(DEFAULT_BENCHMARKS_PATH)
    assert {s.symbol: s.currency for s in cfg.symbols} == {"CSPX.LON": "USD", "XDWD.DEX": "EUR"}


@pytest.mark.parametrize(
    "key",
    ["source", "pause_between_symbols_seconds", "stale_warn_days", "gap_calendar_days",
     "lookback_max_calendar_days", "jump_threshold_pct", "symbols"],
)
def test_missing_key_is_named(tmp_path, key):
    if key == "symbols":
        text = VALID.split("symbols:")[0]
    else:
        text = "\n".join(line for line in VALID.splitlines() if not line.startswith(key + ":"))
    with pytest.raises(BenchmarksConfigError, match=key):
        load_benchmarks(write(tmp_path, text))


@pytest.mark.parametrize(
    "bad",
    [
        ("pause_between_symbols_seconds: 13", "pause_between_symbols_seconds: -1"),
        ("stale_warn_days: 14", "stale_warn_days: 0"),
        ("gap_calendar_days: 6", "gap_calendar_days: 'sechs'"),
        ("jump_threshold_pct: 10", "jump_threshold_pct: 0"),
        ("jump_threshold_pct: 10", "jump_threshold_pct: true"),
    ],
)
def test_invalid_numbers_are_refused(tmp_path, bad):
    with pytest.raises(BenchmarksConfigError):
        load_benchmarks(write(tmp_path, VALID.replace(*bad)))


def test_duplicate_symbol_unknown_currency_and_unknown_key_are_refused(tmp_path):
    with pytest.raises(BenchmarksConfigError, match="CSPX.LON"):
        load_benchmarks(write(tmp_path, VALID.replace("XDWD.DEX", "CSPX.LON")))
    with pytest.raises(BenchmarksConfigError, match="currency"):
        load_benchmarks(write(tmp_path, VALID.replace("currency: EUR", "currency: eur")))
    with pytest.raises(BenchmarksConfigError, match="unrecognized"):
        load_benchmarks(write(tmp_path, VALID + "extra: 1\n"))


def test_missing_file_and_invalid_yaml_are_refused(tmp_path):
    with pytest.raises(BenchmarksConfigError):
        load_benchmarks(tmp_path / "gibt_es_nicht.yaml")
    with pytest.raises(BenchmarksConfigError):
        load_benchmarks(write(tmp_path, "source: [unclosed"))

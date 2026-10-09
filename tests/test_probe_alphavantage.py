import io
import json
import urllib.error

import pytest

import probe_alphavantage as probe


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener_returning(payload, seen=None):
    def opener(url, timeout=None):
        if seen is not None:
            seen.append(url)
        return _Response(json.dumps(payload).encode("utf-8"))

    return opener


GOOD = {
    "Time Series (Daily)": {
        "2026-10-07": {"4. close": "838.10"},
        "2026-10-08": {"4. close": "838.64"},
        "2026-10-06": {"4. close": "836.00"},
    }
}


def test_summarize_reports_range_count_and_last_close():
    line = probe.summarize("CSPX.LON", GOOD)
    assert line == "CSPX.LON: 3 Handelstage, 2026-10-06 bis 2026-10-08, letzter Schlusskurs 838.64"


@pytest.mark.parametrize("field", ["Error Message", "Information", "Note"])
def test_summarize_shows_provider_message_when_no_series(field):
    line = probe.summarize("X.LON", {field: "Tageslimit erreicht"})
    assert line == "X.LON: KEINE DATEN - Tageslimit erreicht"


def test_summarize_unknown_response_is_not_silently_ok():
    assert "KEINE DATEN" in probe.summarize("X.LON", {"foo": 1})


def test_resolve_api_key_reads_env_file_and_does_not_override(tmp_path, monkeypatch):
    for name in (probe.KEY_NAME, "SIMFIN_API_KEY"):
        # setenv+delenv: nach dem Test stellt monkeypatch den urspruenglichen Zustand (fehlt) wieder her,
        # auch wenn load_dotenv_if_present die Variable setzt.
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    env = tmp_path / ".env"
    env.write_text("SIMFIN_API_KEY=a\n# c\nALPHAVANTAGE_API_KEY='geheim'\n", encoding="utf-8")
    monkeypatch.setattr(probe, "DEFAULT_ENV_PATH", env)
    probe.load_dotenv_if_present(env)
    assert probe.resolve_api_key() == "geheim"
    monkeypatch.setenv(probe.KEY_NAME, "aus_umgebung")
    probe.load_dotenv_if_present(env)
    assert probe.resolve_api_key() == "aus_umgebung"


def test_resolve_api_key_missing_raises(monkeypatch, tmp_path):
    monkeypatch.delenv(probe.KEY_NAME, raising=False)
    monkeypatch.setattr(probe, "DEFAULT_ENV_PATH", tmp_path / "gibt_es_nicht")
    # load_dotenv_if_present nutzt den Default-Pfad als Standardargument beim Import;
    # deshalb hier direkt mit leerer Umgebung pruefen.
    monkeypatch.setattr(probe, "load_dotenv_if_present", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        probe.resolve_api_key()


def test_main_one_request_per_symbol_and_key_never_printed(monkeypatch, capsys):
    monkeypatch.setenv(probe.KEY_NAME, "SUPERGEHEIM")
    monkeypatch.setattr(probe, "load_dotenv_if_present", lambda *a, **k: None)
    seen = []
    code = probe.main([], opener=_opener_returning(GOOD, seen))
    out = capsys.readouterr().out
    assert code == 0
    assert len(seen) == 2  # genau ein Abruf je Standard-Symbol
    assert "symbol=CSPX.LON" in seen[0] and "symbol=XDWD.DEX" in seen[1]
    assert "SUPERGEHEIM" not in out


def test_main_network_error_prints_type_only_and_returns_1(monkeypatch, capsys):
    monkeypatch.setenv(probe.KEY_NAME, "SUPERGEHEIM")
    monkeypatch.setattr(probe, "load_dotenv_if_present", lambda *a, **k: None)

    def failing(url, timeout=None):
        raise urllib.error.URLError(f"kaputt {url}")

    code = probe.main(["CSPX.LON"], opener=failing)
    out = capsys.readouterr().out
    assert code == 1
    assert "ABRUF FEHLGESCHLAGEN (URLError)" in out
    assert "SUPERGEHEIM" not in out


def test_main_returns_1_when_provider_sends_no_data(monkeypatch, capsys):
    monkeypatch.setenv(probe.KEY_NAME, "k")
    monkeypatch.setattr(probe, "load_dotenv_if_present", lambda *a, **k: None)
    code = probe.main(["CSPX.LON"], opener=_opener_returning({"Information": "premium"}))
    assert code == 1
    assert "KEINE DATEN" in capsys.readouterr().out

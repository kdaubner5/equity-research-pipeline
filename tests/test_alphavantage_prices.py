"""Alpha-Vantage-Abruf TIME_SERIES_DAILY (compact) fuer die Messlatten-Kurse."""

import io
import json
import urllib.error

import pytest

from data_layer import alphavantage_prices as av


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_for(payload, seen=None):
    def opener(url, timeout=None):
        if seen is not None:
            seen.append((url, timeout))
        return _Response(json.dumps(payload).encode("utf-8"))

    return opener


GOOD = {"Meta Data": {}, "Time Series (Daily)": {"2026-10-07": {"4. close": "838.10"}, "2026-10-08": {"4. close": "838.64"}}}


def test_parse_daily_returns_dates_and_raw_close_values():
    assert av.parse_daily(GOOD, "k") == {"2026-10-07": "838.10", "2026-10-08": "838.64"}


def test_fetch_daily_sends_one_request_with_compact_and_a_timeout():
    seen = []
    result = av.fetch_daily("CSPX.LON", "SECRET", opener=opener_for(GOOD, seen))
    assert len(result) == 2 and len(seen) == 1
    url, timeout = seen[0]
    assert "function=TIME_SERIES_DAILY" in url and "outputsize=compact" in url and "symbol=CSPX.LON" in url
    assert timeout == av.REQUEST_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "payload, kind",
    [
        ({"Information": "Burst pattern detected. Please spread out your requests; 1 request per second."}, "limit"),
        ({"Note": "Thank you ... 25 requests per day ..."}, "limit"),
        ({"Information": "This is a premium endpoint"}, "error"),
        ({"Error Message": "Invalid API call."}, "error"),
        ({"foo": 1}, "format"),
        ([1, 2], "format"),
        ({"Time Series (Daily)": {}}, "format"),
    ],
)
def test_error_kinds(payload, kind):
    with pytest.raises(av.AlphaVantageError) as info:
        av.parse_daily(payload, "k")
    assert info.value.kind == kind


def test_provider_message_has_the_key_removed():
    with pytest.raises(av.AlphaVantageError) as info:
        av.parse_daily({"Information": "bad key SECRET please"}, "SECRET")
    assert "SECRET" not in str(info.value) and "***" in str(info.value)


def test_http_error_network_error_and_bad_json_become_typed_errors_without_the_key():
    def http_error(url, timeout=None):
        raise urllib.error.HTTPError(url, 429, "x", {}, None)

    def reset(url, timeout=None):
        raise ConnectionResetError("x")

    def not_json(url, timeout=None):
        return _Response(b"<html>")

    for opener in (http_error, reset, not_json):
        with pytest.raises(av.AlphaVantageError) as info:
            av.fetch_daily("CSPX.LON", "SECRET", opener=opener)
        assert info.value.kind == "network" and "SECRET" not in str(info.value)
    with pytest.raises(av.AlphaVantageError) as info:
        av.fetch_daily("CSPX.LON", "SECRET", opener=http_error)
    assert "429" in str(info.value)

"""Shared fakes for the universe tests (test_simfin_universe.py,
test_run_universe.py): a scripted fake SimFin behind a monkeypatched
urllib.request.urlopen, a fake clock (time only passes through sleep()) and
builders for price/general/list records. Nothing here touches the network."""

import io
import json
import urllib.error
from datetime import date, timedelta

import pytest

from data_layer import simfin_universe as su

AS_OF = date(2026, 9, 24)
WINDOW = 60
PRICE_COLUMNS = [
    "Date",
    "Dividend Paid",
    "Common Shares Outstanding",
    "Last Closing Price",
    "Adjusted Closing Price",
    "Highest Price",
    "Lowest Price",
    "Opening Price",
    "Trading Volume",
]


class FakeClock:
    """Time only passes through sleep(), so spacing/retry waits are observable."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeResponse(io.BytesIO):
    def __init__(self, payload, headers=None, status=200):
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.headers = headers if headers is not None else {}
        self.status = status


def price_record(company_id, *, currency="USD", days=3, close=10.0, volume=1000, shares=5_000_000, last=None):
    last = last or AS_OF - timedelta(days=1)
    data = []
    for i in range(days):
        d = last - timedelta(days=days - 1 - i)
        data.append([d.isoformat(), None, shares, close, close, close, close, close, volume])
    return {
        "name": f"CO{company_id}",
        "id": company_id,
        "ticker": f"T{company_id}",
        "currency": currency,
        "isin": None,
        "columns": PRICE_COLUMNS,
        "data": data,
    }


class FakeSimFin:
    """Scripted SimFin: prices for `records` ids only (absent ids stay absent,
    like the real API), optional per-call-number failures."""

    def __init__(self, monkeypatch, records=None, general=None, companies_list=None):
        self.records = dict(records or {})
        self.general = general
        self.companies_list = companies_list
        self.requests = []  # (endpoint, [ids])
        self.fail_calls = {}  # 1-based call number -> exception or HTTP code
        self.hide_in_batches = set()  # ids that only show up when requested alone
        self.shuffle = False
        self.extra_id_in_batch = None
        self.headers = {"X-Rate-Limit-Remaining": "0"}
        monkeypatch.setattr(su.urllib.request, "urlopen", self._urlopen)

    def _urlopen(self, req, timeout=None):
        url = req.full_url
        call_number = len(self.requests) + 1
        if url.startswith(su.SIMFIN_PRICES_URL):
            params = dict(part.split("=", 1) for part in url.split("?", 1)[1].split("&"))
            ids = [int(i) for i in params["id"].split(",")]
            self.requests.append(("prices", ids))
        elif url == su.SIMFIN_GENERAL_BULK_URL:
            self.requests.append(("general", []))
        else:
            assert url == su.SIMFIN_COMPANIES_LIST_URL, url
            self.requests.append(("list", []))
        failure = self.fail_calls.get(call_number)
        if isinstance(failure, int):
            raise urllib.error.HTTPError(url, failure, "scripted", {"X-Rate-Limit-Remaining": "0"}, io.BytesIO(b"{}"))
        if failure is not None:
            raise failure
        if url.startswith(su.SIMFIN_PRICES_URL):
            out = [
                self.records[i]
                for i in ids
                if i in self.records and not (len(ids) > 1 and i in self.hide_in_batches)
            ]
            if self.shuffle:
                out.reverse()
            if self.extra_id_in_batch is not None and len(ids) > 1:
                out.append(price_record(self.extra_id_in_batch))
            return FakeResponse(out, self.headers)
        return FakeResponse(self.general if url == su.SIMFIN_GENERAL_BULK_URL else self.companies_list, self.headers)

    def price_requests(self):
        return [ids for endpoint, ids in self.requests if endpoint == "prices"]



GENERAL_COLUMNS = ["companyDescription", "endFy", "id", "industryName", "isin", "market", "name", "numEmployees", "sectorCode", "sectorName", "ticker"]


def general_row(company_id, ticker, *, market="US", sector=(106, "Healthcare", "Drug Manufacturers")):
    code, industry, sector_name = sector if sector else (None, None, None)
    return [None, 12, company_id, industry, None, market, f"Company {company_id}", None, code, sector_name, ticker]


def general_response(rows):
    return {"columns": GENERAL_COLUMNS, "data": rows}


def list_response(rows):
    return [
        {"id": r[2], "industryName": r[3], "isin": None, "name": r[6], "sectorCode": r[8], "sectorName": r[9], "ticker": r[10]}
        for r in rows
    ]


@pytest.fixture
def clock():
    return FakeClock()

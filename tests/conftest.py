import io
import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "companies"


class FakeUrlopenResponse(io.BytesIO):
    """A urllib.request.urlopen() response stand-in for offline network mocks
    (test_simfin_client.py, test_run_stage1.py) - a context-managed,
    read()-able byte stream is all json.load(resp) needs."""


def load_company(company_id):
    """Load a company fixture by id, periods sorted by fiscal_year."""
    with open(FIXTURES_DIR / f"{company_id}.json") as f:
        data = json.load(f)
    data["periods"] = sorted(data["periods"], key=lambda p: p["fiscal_year"])
    return data


def all_company_ids():
    """All fixture company_ids, sorted by ticker."""
    return sorted(path.stem for path in FIXTURES_DIR.glob("*.json"))

import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "companies"


def load_company(company_id):
    """Load a company fixture by id, periods sorted by fiscal_year."""
    with open(FIXTURES_DIR / f"{company_id}.json") as f:
        data = json.load(f)
    data["periods"] = sorted(data["periods"], key=lambda p: p["fiscal_year"])
    return data


def all_company_ids():
    """All fixture company_ids, sorted by ticker."""
    return sorted(path.stem for path in FIXTURES_DIR.glob("*.json"))

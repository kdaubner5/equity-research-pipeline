import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "companies"


def load_company(company_id):
    """Load a company fixture by id, periods sorted by fiscal_year."""
    with open(FIXTURES_DIR / f"{company_id}.json") as f:
        data = json.load(f)
    data["periods"] = sorted(data["periods"], key=lambda p: p["fiscal_year"])
    return data

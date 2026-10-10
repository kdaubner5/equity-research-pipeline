""".env-Datei lesen (Schluessel liegen nur dort oder in der Umgebung, nie im Repo).

Kleiner, abhaengigkeitsfreier Parser wie in run_stage1.py. Bewusst eigenstaendig (und nicht aus dem Einmal-Skript
probe_alphavantage.py importiert), damit das Produktivskript nicht von einem Wegwerf-Skript abhaengt.
"""

import os
from pathlib import Path

KEY_NAME = "ALPHAVANTAGE_API_KEY"
DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"


def load_dotenv_if_present(path=None):
    """KEY=VALUE aus .env in os.environ, ohne bestehende Variablen zu ueberschreiben. Keine Datei: nichts tun."""
    path = Path(path) if path is not None else DEFAULT_ENV_PATH
    if not path.exists():
        return
    # utf-8-sig: eine unter Windows gespeicherte .env kann mit einem unsichtbaren BOM beginnen.
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        os.environ.setdefault(key, value.strip().strip("'\""))


def resolve_api_key():
    """ALPHAVANTAGE_API_KEY aus Umgebung oder .env; RuntimeError ohne den Wert zu nennen, wenn er fehlt."""
    load_dotenv_if_present()
    key = os.environ.get(KEY_NAME, "").strip()
    if not key:
        raise RuntimeError(f"{KEY_NAME} fehlt (weder Umgebung noch .env).")
    return key

""".env-Datei lesen (Schluessel liegen nur dort oder in der Umgebung, nie im Repo).

Kleiner, abhaengigkeitsfreier Parser wie in run_stage1.py. Bewusst eigenstaendig (und nicht aus dem Einmal-Skript
probe_alphavantage.py importiert), damit das Produktivskript nicht von einem Wegwerf-Skript abhaengt.
"""

import os
from pathlib import Path

KEY_NAME = "ALPHAVANTAGE_API_KEY"
DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
DEFAULT_KEY_FILES = (DEFAULT_ENV_PATH.with_name(".env.alphavantage"), DEFAULT_ENV_PATH)


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


def read_key_from_file(path, key_name=KEY_NAME):
    """Nur den Wert von `key_name` aus einer KEY=VALUE-Datei; andere Schluessel (z. B. SimFin) werden nicht in die
    Umgebung geladen. None, wenn die Datei oder der Schluessel fehlt."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise RuntimeError(f"{path.name} ist nicht lesbar") from None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key == key_name:
            return value.strip().strip("'\"") or None
    return None


def resolve_api_key(files=None):
    """ALPHAVANTAGE_API_KEY aus der Umgebung, sonst aus .env.alphavantage, sonst aus .env (dort nur diese eine Zeile).
    RuntimeError ohne den Wert zu nennen, wenn er fehlt. Auf dem Server liegt nur .env.alphavantage."""
    key = os.environ.get(KEY_NAME, "").strip()
    if key:
        return key
    for path in (DEFAULT_KEY_FILES if files is None else files):
        key = read_key_from_file(path)
        if key:
            return key
    raise RuntimeError(f"{KEY_NAME} fehlt (weder Umgebung noch .env.alphavantage noch .env).")

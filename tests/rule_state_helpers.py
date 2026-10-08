"""Helpers for the runner tests that check which rule-file content a run really stored."""

import hashlib
import sqlite3
from pathlib import Path


def custom_copy(tmp_path, source, name=None):
    """A valid copy of a config file with a trailing comment, so its bytes (and hash) differ from the repo's file."""
    target = Path(tmp_path) / ("custom_" + (name or Path(source).name))
    target.write_bytes(Path(source).read_bytes() + b"\n# custom copy for the rule-state test\n")
    return target


def expected_sha(path):
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def stored_sha(db_path, relative):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT sha256 FROM run_rule_files WHERE path = ?", (relative,)).fetchone()[0]
    finally:
        conn.close()

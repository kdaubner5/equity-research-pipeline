"""Rule state per run (docs/decisions.md, "Regelstand und Kurs je Lauf im Archiv", Roadmap A.1).

What a run was computed with: the code commit, a marker for uncommitted changes (clean / dirty /
unknown), and the *content* of the rule files (not only a hash). Read-only towards git (rev-parse, status);
nothing is written to the repository. The tables are new and created with CREATE TABLE IF NOT EXISTS, so no
existing table is touched. Older runs have no row here - "not recorded", never back-filled.

The list of rule files and the limit on stored paths are constants on purpose (decision, question 4): they
secure the archive's trail and are not meant to be reconfigurable.
"""

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

RULE_FILES = (
    "config/thresholds.yaml",
    "config/stage2.yaml",
    "config/trend_guard.yaml",
    "config/sector_profiles.yaml",
)
# Files in config/ that are deliberately not screening rules (guard of the agent, pilot list). A test fails
# when a new file in config/ is in neither tuple, so a new rule file cannot be forgotten silently.
NON_RULE_CONFIG_FILES = ("config/bash_guard.yaml", "config/universe.txt")

STEPS = ("prefilter", "stage1", "stage2", "trend")
MAX_DIRTY_PATHS = 50
GIT_TIMEOUT_SECONDS = 30

CODE_CLEAN = "clean"
CODE_DIRTY = "dirty"
CODE_UNKNOWN = "unknown"

_UNTRACKED_RELEVANT = re.compile(r"^(config/|src/|run_[^/]*\.py$)", re.IGNORECASE)
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class RuleStateError(Exception):
    """A rule file is missing or not readable - the run must stop before anything is written."""


@dataclass(frozen=True)
class RuleFile:
    path: str  # relative, forward slashes, as in RULE_FILES
    sha256: str  # of the normalized bytes (line endings CRLF -> LF), the bytes that are stored
    content: str


@dataclass(frozen=True)
class RuleState:
    git_commit: str | None  # 40 hex characters, None when it could not be read
    code_state: str  # CODE_CLEAN / CODE_DIRTY / CODE_UNKNOWN
    dirty_paths: tuple  # full list; truncated only when stored
    files: tuple  # RuleFile, in RULE_FILES order


def _run_git(root, args):
    """(returncode, stdout bytes). Any failure to run git (missing, timeout) is returncode 127 with no output."""
    try:
        # --no-optional-locks: `git status` must not refresh the index (read-only towards the repository)
        completed = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), *args], capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return 127, b""
    return completed.returncode, completed.stdout


def _parse_status(output):
    """Dirty paths from `git status --porcelain -z`: tracked changes always count, untracked files only
    inside config/, src/ or as run_*.py in the root (private scripts in the root must not make every run dirty)."""
    tokens = output.split(b"\0")
    paths = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4:
            continue
        status = token[:2].decode("ascii", "replace")
        path = token[3:].decode("utf-8", "replace")
        if "R" in status or "C" in status:
            index += 1  # the original path of a rename/copy follows as a separate token
        if status == "!!":
            continue
        if status == "??" and not _UNTRACKED_RELEVANT.match(path):
            continue
        paths.append(path)
    return tuple(paths)


def _read_rule_file(relative, path):
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise RuleStateError(f"rule file {relative} not readable ({path}): {exc}") from exc
    normalized = raw.replace(b"\r\n", b"\n")  # the same normalization as the existing config hashes
    try:
        content = normalized.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuleStateError(f"rule file {relative} is not valid UTF-8 ({path}): {exc}") from exc
    return RuleFile(path=relative, sha256=hashlib.sha256(normalized).hexdigest(), content=content)


def capture_rule_state(repo_root=REPO_ROOT, *, rule_paths=None, git_runner=None):
    """Read the rule state once, at the start of a run.

    rule_paths maps a relative rule-file name (an entry of RULE_FILES) to the path the run actually uses, so the
    stored content is the content the run loaded (default: the file below repo_root). git_runner(args) ->
    (returncode, stdout) is replaceable for tests. Raises RuleStateError for a missing or unreadable rule file.
    A git failure is not an error: the state is then CODE_UNKNOWN (and git_commit None if the commit itself
    could not be read) - never a silent 'clean'.
    """
    root = Path(repo_root)
    rule_paths = dict(rule_paths or {})
    unknown = sorted(set(rule_paths) - set(RULE_FILES))
    if unknown:
        raise ValueError(f"not rule files: {unknown}")
    files = tuple(_read_rule_file(relative, rule_paths.get(relative, root / relative)) for relative in RULE_FILES)

    run = git_runner or (lambda args: _run_git(root, args))
    code, out = run(["rev-parse", "HEAD"])
    commit = out.decode("ascii", "replace").strip() if code == 0 else ""
    if not _COMMIT.match(commit):
        return RuleState(git_commit=None, code_state=CODE_UNKNOWN, dirty_paths=(), files=files)
    # --untracked-files=normal: a user's status.showUntrackedFiles=no must not turn an untracked rule file into 'clean'
    code, out = run(["status", "--porcelain", "-z", "--untracked-files=normal"])
    if code != 0:
        return RuleState(git_commit=commit, code_state=CODE_UNKNOWN, dirty_paths=(), files=files)
    dirty = _parse_status(out)
    return RuleState(git_commit=commit, code_state=CODE_DIRTY if dirty else CODE_CLEAN, dirty_paths=dirty, files=files)


def require_same_bytes(rule_state, relative, expected_sha256):
    """Raise RuleStateError when the stored content of a rule file is not the file whose hash the run recorded.

    The runners load a rule file, hash it for the archive and capture its content in separate reads; this check
    makes sure the same bytes went into all of them (a file changed in between stops the run, nothing is written).
    """
    for rule_file in rule_state.files:
        if rule_file.path == relative:
            if rule_file.sha256 != expected_sha256:
                raise RuleStateError(
                    f"rule file {relative} changed while the run started: stored content sha256 {rule_file.sha256[:12]} "
                    f"differs from the config hash {expected_sha256[:12]} the run recorded"
                )
            return
    raise ValueError(f"not a captured rule file: {relative}")


def init_rule_state_tables(connection):
    """Create the three rule-state tables if they do not exist (idempotent, no commit)."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS run_rule_state (
            step TEXT NOT NULL,
            run_timestamp TEXT NOT NULL,
            git_commit TEXT,
            code_state TEXT NOT NULL,
            dirty_paths TEXT,
            PRIMARY KEY (step, run_timestamp)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS run_rule_files (
            step TEXT NOT NULL,
            run_timestamp TEXT NOT NULL,
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            PRIMARY KEY (step, run_timestamp, path)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS rule_file_contents (
            sha256 TEXT PRIMARY KEY,
            content TEXT NOT NULL
        )
        """
    )


def _stored_dirty_paths(paths):
    if not paths:
        return None
    shown = list(paths[:MAX_DIRTY_PATHS])
    if len(paths) > MAX_DIRTY_PATHS:
        shown.append(f"(und {len(paths) - MAX_DIRTY_PATHS} weitere)")
    return "\n".join(shown)


def write_rule_state(connection, step, run_timestamp, rule_state):
    """Write one run's rule state on `connection`, WITHOUT committing - the caller commits it together with the
    run's results (all or nothing). A repeated (step, run_timestamp) raises sqlite3.IntegrityError like the
    results do; identical file content in several runs is stored once."""
    if step not in STEPS:
        raise ValueError(f"unknown step {step!r}; expected one of {STEPS}")
    init_rule_state_tables(connection)
    connection.execute(
        "INSERT INTO run_rule_state (step, run_timestamp, git_commit, code_state, dirty_paths) VALUES (?, ?, ?, ?, ?)",
        (step, run_timestamp, rule_state.git_commit, rule_state.code_state, _stored_dirty_paths(rule_state.dirty_paths)),
    )
    for rule_file in rule_state.files:
        connection.execute(
            "INSERT OR IGNORE INTO rule_file_contents (sha256, content) VALUES (?, ?)", (rule_file.sha256, rule_file.content)
        )
        connection.execute(
            "INSERT INTO run_rule_files (step, run_timestamp, path, sha256) VALUES (?, ?, ?, ?)",
            (step, run_timestamp, rule_file.path, rule_file.sha256),
        )

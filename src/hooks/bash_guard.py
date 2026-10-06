"""PreToolUse hook for the Bash tool (Issue #60, Stufe 2, docs/bash-guard.md).

Claude Code runs this script before every Bash call, in every permission
mode (default, auto, bypassPermissions): the command arrives as JSON on
stdin ({"tool_name": "Bash", "tool_input": {"command": ...}, "cwd": ...}).
Exit code 2 blocks the call and shows stderr to the model; exit code 0 lets
the normal permission flow continue.

The hook refuses commands that would push to / merge into / delete or
locally change a protected branch (config/bash_guard.yaml: master, main) in
any spelling it can resolve, plus the "stage everything" forms of git add,
gh pr merge / gh api writes and writes to the hook itself. It parses the
command with a shell tokenizer instead of matching text, so quoting
(ma''ster), wrappers (git -C ., bash -c, env, timeout, xargs), redirections,
refspecs (HEAD:master, +ref, :ref), option abbreviations (--forc) and nested
strings are handled. What it cannot see (script files, pytest, obfuscated
code) is listed in docs/bash-guard.md.

Fail closed: an unreadable config, invalid input, a parse error, an
exceeded time budget or any internal error refuses the call (exit 2).
Patterns, limits and time budgets live in config/bash_guard.yaml.
"""

import json
import math
import posixpath
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote

import yaml

EXIT_ALLOW = 0
EXIT_BLOCK = 2
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "bash_guard.yaml"

_OPERATOR_CHARS = frozenset(";&|()<>")
_REDIRECT = re.compile(r"^(&>>?|[<>]+&?|>\|)$")
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_SHELL_KEYWORDS = frozenset(
    ["!", "{", "}", "if", "then", "elif", "else", "do", "while", "until", "time", "coproc", "function", "select", "case", "in", "esac", "fi", "done"]
)
_HEREDOC_DELIM = re.compile(r"-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_-]*)\1")


class GuardConfigError(ValueError):
    """config/bash_guard.yaml is missing, unreadable or malformed."""


class GuardError(RuntimeError):
    """The command could not be analysed with certainty (fail closed)."""


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def _require(mapping, key, kind, where):
    if not isinstance(mapping, dict) or key not in mapping:
        raise GuardConfigError(f"{where}: missing key '{key}'")
    value = mapping[key]
    if not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
        raise GuardConfigError(f"{where}.{key}: expected {kind}, got {type(value).__name__}")
    return value


def _number(mapping, key, where):
    value = _require(mapping, key, (int, float), where)
    if not math.isfinite(value) or value <= 0:
        raise GuardConfigError(f"{where}.{key}: must be a finite number > 0")
    return value


def _str_list(mapping, key, where, allow_empty=False):
    value = _require(mapping, key, list, where)
    if not allow_empty and not value:
        raise GuardConfigError(f"{where}.{key}: must not be empty")
    for item in value:
        if not isinstance(item, str) or not item:
            raise GuardConfigError(f"{where}.{key}: entries must be non-empty strings")
    return value


def _regex_list(mapping, key, where):
    patterns = _str_list(mapping, key, where)
    try:
        return [re.compile(p) for p in patterns]
    except re.error as exc:
        raise GuardConfigError(f"{where}.{key}: invalid regex ({exc})") from exc


def _list_map(mapping, key, where):
    value = _require(mapping, key, dict, where)
    result = {}
    for name, items in value.items():
        result[name] = _str_list(value, name, f"{where}.{key}")
    return result


class Config:
    """Validated, compiled view of config/bash_guard.yaml."""

    def __init__(self, data):
        if not isinstance(data, dict):
            raise GuardConfigError("config root must be a mapping")
        self.protected = [b.casefold() for b in _str_list(data, "protected_branches", "config")]
        self.protected_word = re.compile(
            r"(?<![\w.-])(?:%s)(?![\w.-])" % "|".join(re.escape(b) for b in self.protected), re.IGNORECASE
        )
        self.max_command_length = _require(data, "max_command_length", int, "config")
        self.max_depth = _require(data, "max_depth", int, "config")
        if self.max_command_length < 1 or self.max_depth < 1:
            raise GuardConfigError("config: max_command_length and max_depth must be >= 1")
        limits = _require(data, "limits", dict, "config")
        self.analysis_seconds = _number(limits, "analysis_seconds", "limits")
        self.hook_timeout_seconds = _number(limits, "hook_timeout_seconds", "limits")
        if self.analysis_seconds >= self.hook_timeout_seconds:
            # Claude Code lets a call through when the hook runs into its own timeout
            raise GuardConfigError("limits.analysis_seconds must be smaller than limits.hook_timeout_seconds")
        self.git_lookup_seconds = _number(limits, "git_lookup_seconds", "limits")
        self.min_abbreviation = _require(limits, "min_long_option_abbreviation", int, "limits")
        if self.min_abbreviation < 2:
            raise GuardConfigError("limits.min_long_option_abbreviation must be >= 2")
        self.non_executing = set(_str_list(data, "non_executing_commands", "config"))
        self.dynamic_arg = set(_str_list(data, "dynamic_arg_commands", "config"))
        self.wrappers = set(_str_list(data, "command_string_wrappers", "config"))
        self.env_prefixes = _str_list(data, "refused_env_prefixes", "config")
        self.env_setting_commands = set(_str_list(data, "env_setting_commands", "config"))
        self.refused_commands = set(_str_list(data, "refused_commands", "config"))
        self.cwd_changing_commands = set(_str_list(data, "cwd_changing_commands", "config"))
        self.protected_paths = _regex_list(data, "protected_paths", "config")
        self.path_writers = set(_str_list(data, "path_writing_commands", "config"))
        self.path_writers_with_option = _list_map(data, "path_writing_with_option", "config")
        self.shells = set(_str_list(data, "shell_interpreters", "config"))
        self.script_interpreters = _regex_list(data, "script_interpreter_patterns", "config")
        self.inline_markers = _regex_list(data, "inline_exec_markers", "config")
        self.inline_deny = _regex_list(data, "inline_deny_patterns", "config")

        git = _require(data, "git", dict, "config")
        self.git_global_with_value = set(_str_list(git, "global_options_with_value", "git"))
        self.git_allowed_c_prefixes = [p.casefold() for p in _str_list(git, "allowed_c_key_prefixes", "git")]
        self.git_known = set(_str_list(git, "known_subcommands", "git"))
        self.git_refused_options = _list_map(git, "refused_options_by_subcommand", "git")
        self.git_refused_words = {k: {w.casefold() for w in v} for k, v in _list_map(git, "refused_words_by_subcommand", "git").items()}
        push = _require(git, "push", dict, "git")
        self.push_long = _str_list(push, "refused_long_options", "git.push")
        self.push_short = set(_str_list(push, "refused_short_letters", "git.push"))
        self.push_with_value = set(_str_list(push, "options_with_value", "git.push"))
        self.push_dst_allowed = _str_list(push, "allowed_destination_prefixes", "git.push")
        self.push_dst_refused = _str_list(push, "refused_destination_prefixes", "git.push")
        self.push_words = set(_str_list(push, "refused_refspec_words", "git.push"))
        add = _require(git, "add", dict, "git")
        self.add_long = _str_list(add, "refused_long_options", "git.add")
        self.add_short = set(_str_list(add, "refused_short_letters", "git.add"))
        self.pathspec_refused = _regex_list(add, "refused_pathspec_patterns", "git.add")
        self.pathspec_sensitive = _regex_list(add, "sensitive_pathspec_patterns", "git.add")
        commit = _require(git, "commit", dict, "git")
        self.commit_short = set(_str_list(commit, "refused_short_letters", "git.commit"))
        self.commit_long = _str_list(commit, "refused_long_options", "git.commit")
        self.commit_with_value = set(_str_list(commit, "options_with_value", "git.commit"))
        self.commit_value_letters = set(_str_list(commit, "short_letters_with_value", "git.commit"))
        local = _require(git, "local", dict, "git")
        self.local_on_protected = set(_str_list(local, "refused_subcommands_on_protected_branch", "git.local"))
        self.local_pull_option = _require(local, "pull_required_option", str, "git.local")
        self.local_options_naming = _list_map(local, "refused_options_naming_protected_branch", "git.local")
        self.local_naming_subcommands = set(_str_list(local, "refused_subcommands_naming_protected_branch", "git.local"))
        self.local_switching = set(_str_list(local, "branch_switching_subcommands", "git.local"))
        self.local_refused = set(_str_list(local, "refused_subcommands", "git.local"))

        gh = _require(data, "gh", dict, "config")
        self.gh_global_with_value = set(_str_list(gh, "global_options_with_value", "gh"))
        pairs = _require(gh, "refused_subcommands", list, "gh")
        self.gh_refused = set()
        for pair in pairs:
            if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(p, str) for p in pair)):
                raise GuardConfigError("gh.refused_subcommands: entries must be [word, word]")
            self.gh_refused.add((pair[0].casefold(), pair[1].casefold()))
        api = _require(gh, "api", dict, "gh")
        self.api_write_methods = {m.upper() for m in _str_list(api, "write_methods", "gh.api")}
        self.api_field_options = set(_str_list(api, "field_options", "gh.api"))
        self.api_refuse_all_writes = _require(api, "refuse_all_writes", bool, "gh.api")
        self.api_refused_args = _regex_list(api, "refused_argument_patterns", "gh.api")
        self.api_header_patterns = _regex_list(api, "write_header_patterns", "gh.api")
        self.api_read_methods = {m.upper() for m in _str_list(api, "read_methods", "gh.api")}

        http = _require(data, "http", dict, "config")
        self.http_clients = set(_str_list(http, "clients", "http"))
        self.http_hosts = [h.casefold() for h in _str_list(http, "github_hosts", "http")]
        self.http_write_long = _str_list(http, "write_long_options", "http")
        self.http_write_short = set(_str_list(http, "write_short_letters", "http"))
        self.http_method_letters = set(_str_list(http, "method_short_letters", "http"))
        self.http_read_methods = {m.upper() for m in _str_list(http, "read_methods", "http")}
        self.http_refused_options = _str_list(http, "refused_options", "http")
        self.httpie_clients = set(_str_list(http, "httpie_clients", "http"))
        self.httpie_write_patterns = _regex_list(http, "httpie_write_patterns", "http")


def load_config(path=DEFAULT_CONFIG_PATH):
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise GuardConfigError(f"cannot read {path}: {exc}") from exc
    return Config(data)


# --------------------------------------------------------------------------
# resolvers (replaceable in tests)
# --------------------------------------------------------------------------

def _git_output(cdirs, cwd, args, timeout):
    command = ["git"]
    for directory in cdirs:
        command += ["-C", directory]
    command += list(args)
    try:
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GuardError(f"git lookup failed: {exc}") from exc


def current_branch(cdirs, cwd, timeout):
    """Branch HEAD points to, or None on a detached HEAD / outside a repo."""
    result = _git_output(cdirs, cwd, ["symbolic-ref", "--short", "-q", "HEAD"], timeout)
    if result.returncode == 0:
        return result.stdout.strip() or None
    if result.returncode == 1:
        return None  # detached HEAD
    if result.returncode == 128 and not cdirs:
        return None  # the working directory is not a repository: nothing to push
    # with -C <dir> the directory may be created earlier in the same command
    # (git worktree add, git clone): its branch is unknown
    raise GuardError(f"git symbolic-ref failed with exit {result.returncode} (branch of the -C directory unknown)")


def git_alias(name, cdirs, cwd, timeout):
    """Value of git config alias.<name>, or None."""
    result = _git_output(cdirs, cwd, ["config", "--get", f"alias.{name}"], timeout)
    if result.returncode == 0:
        return result.stdout.strip() or None
    if result.returncode in (1, 128):
        return None
    raise GuardError(f"git config failed with exit {result.returncode}")


# --------------------------------------------------------------------------
# tokenizing
# --------------------------------------------------------------------------

def _split_heredocs(command):
    """Cut heredoc bodies out of the command text.

    Returns (text without bodies, [(introducer line, body)]). Only a `<<`
    outside quotes starts a heredoc, so a quoted `<<` cannot hide the lines
    after it from the analysis."""
    out = []
    bodies = []
    i = 0
    n = len(command)
    quote = None
    pending = []  # (delimiter, strip_tabs) read from the current line
    line_start = 0
    while i < n:
        ch = command[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                out.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(command[i + 1])
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if command.startswith("<<", i) and not command.startswith("<<<", i):
            match = _HEREDOC_DELIM.match(command, i + 2)
            if match:
                pending.append((match.group(2), command[i + 2:i + 3] == "-"))
                out.append(command[i:match.end()])
                i = match.end()
                continue
        if ch == "\n":
            out.append(ch)
            i += 1
            line = command[line_start:i - 1]
            for delimiter, strip_tabs in pending:
                body_lines = []
                while i < n:
                    end = command.find("\n", i)
                    text = command[i:] if end == -1 else command[i:end]
                    i = n if end == -1 else end + 1
                    if (text.lstrip("\t") if strip_tabs else text) == delimiter:
                        break
                    body_lines.append(text)
                bodies.append((line, "\n".join(body_lines)))
            pending = []
            line_start = i
            continue
        out.append(ch)
        i += 1
    return "".join(out), bodies


def _neutralize(text):
    """Make newlines and backticks visible to the tokenizer.

    A newline ends a command; a backtick opens a command substitution just
    like $( does: it is replaced by ` $( ` so an argument built from it still
    contains a `$` (an unresolved expansion) and the segment is split. Inside
    single quotes bash treats both literally, so they are left alone there
    (markdown with `code` in a `gh ... --body '...'` text must not be read as
    a command)."""
    out = []
    in_single = False
    in_double = False
    in_comment = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_comment:
            # a comment ends at the line end even when the line ends in a
            # backslash (`echo x #\` + newline + `git push ...` runs the push)
            out.append(" ; " if ch == "\n" else ch)
            in_comment = ch != "\n"
        elif in_single:
            # `$(` is literal text in single quotes: break the pair so the
            # substitution extraction does not see it (_analyze removes the
            # marker again when the string is re-analysed as a script, e.g.
            # the argument of bash -c, where it IS code)
            out.append("$\x00" if ch == "$" and text[i + 1:i + 2] == "(" else ch)
            in_single = ch != "'"
        elif ch == "\\" and i + 1 < len(text):
            if text[i + 1] == "\n":
                out.append(" ")  # line continuation
            else:
                out.append(ch)
                out.append(text[i + 1])
            i += 1
        elif ch == "#" and not in_double and (i == 0 or text[i - 1] in " \t\n;&|()<>"):
            in_comment = True
            out.append(ch)
        elif ch == "'" and not in_double:
            in_single = True
            out.append(ch)
        elif ch == '"':
            in_double = not in_double
            out.append(ch)
        elif ch == "`":
            out.append(" $( ")
        elif ch == "\n" and in_double:
            # a newline inside double quotes is data (or the line break of a
            # heredoc inside "$(cat <<'EOF' ... EOF)"): keep it, the
            # substitution is analysed from its own text
            out.append(ch)
        elif ch == "\n":
            out.append(" ; ")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _tokenize(text):
    text = _neutralize(text.replace("\r", " "))
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError as exc:
        raise GuardError(f"command cannot be parsed ({exc})") from exc


def _is_operator(token):
    return bool(token) and all(ch in _OPERATOR_CHARS for ch in token)


def _segments(tokens):
    """Yield (segment, redirect targets).

    `;`, `&&`, `||`, `|`, `&` and parentheses end a segment. A redirection
    (`>`, `2>&1`, `<`, `<<<`, `&>`) does not: the operator, its file
    descriptor number and its target word are removed from the segment so
    `git push 2>&1 origin master` is still one git command. The targets of
    output redirections are returned separately (they are written files)."""
    segment = []
    targets = []
    i = 0
    n = len(tokens)
    while i < n:
        token = tokens[i]
        if _is_operator(token):
            following = tokens[i + 1] if i + 1 < n else None
            if _REDIRECT.match(token) and following is not None and not _is_operator(following):
                if segment and segment[-1].isdigit():
                    segment.pop()
                if ">" in token:
                    targets.append(following)
                i += 2
                continue
            if _REDIRECT.match(token) and following is None:
                i += 1
                continue
            if segment or targets:
                yield segment, targets
            segment, targets = [], []
        else:
            segment.append(token)
        i += 1
    if segment or targets:
        yield segment, targets


def _basename(token):
    return posixpath.basename(token.rstrip("/")) if token else token


def _substitutions(token):
    """Bodies of the $( ... ) command substitutions inside one token (a
    double-quoted string such as "$(git push origin master)" is a single
    token, so the substitution has to be cut out of it)."""
    bodies = []
    start = token.find("$(")
    while start != -1:
        depth = 1
        i = start + 2
        while i < len(token) and depth:
            if token[i] == "(":
                depth += 1
            elif token[i] == ")":
                depth -= 1
            i += 1
        bodies.append(token[start + 2:i - 1] if depth == 0 else token[start + 2:])
        start = token.find("$(", i if depth == 0 else len(token))
    return bodies


def _has_expansion(token):
    return "$" in token or "`" in token


# --------------------------------------------------------------------------
# the guard
# --------------------------------------------------------------------------

class Guard:
    def __init__(self, config, cwd=None, branch_resolver=None, alias_resolver=None, clock=time.monotonic):
        self.config = config
        self.cwd = cwd
        self._branch_resolver = branch_resolver
        self._alias_resolver = alias_resolver
        self._clock = clock
        self._deadline = None
        self._branch_cache = {}
        self._alias_cache = {}
        self._branch_switched = False  # an earlier part of the command may have switched the branch
        self._branch_override = None  # ... or created a new (unprotected) one
        self._cwd_changed = False  # an earlier part of the command changed the directory

    def check(self, command):
        """List of reasons the command is refused (empty = allowed)."""
        reasons = []
        self._branch_cache = {}
        self._alias_cache = {}
        self._branch_switched = False
        self._branch_override = None
        self._cwd_changed = False
        self._deadline = self._clock() + self.config.analysis_seconds
        try:
            self._analyze(command, 0, reasons)
        except GuardError as exc:
            reasons.append(str(exc))
        return reasons

    # -- time budget, cached lookups ----------------------------------------

    def _tick(self):
        if self._deadline is not None and self._clock() > self._deadline:
            raise GuardError(f"analysis took longer than {self.config.analysis_seconds} s (refused)")

    def _lookup_timeout(self):
        remaining = (self._deadline - self._clock()) if self._deadline is not None else self.config.git_lookup_seconds
        return max(0.1, min(self.config.git_lookup_seconds, remaining))

    def _branch(self, cdirs):
        key = tuple(cdirs)
        if key not in self._branch_cache:
            if self._branch_resolver:
                self._branch_cache[key] = self._branch_resolver(list(cdirs))
            else:
                self._branch_cache[key] = current_branch(list(cdirs), self.cwd, self._lookup_timeout())
        return self._branch_cache[key]

    def _alias(self, name, cdirs):
        key = (name, tuple(cdirs))
        if key not in self._alias_cache:
            if self._alias_resolver:
                self._alias_cache[key] = self._alias_resolver(name, list(cdirs))
            else:
                self._alias_cache[key] = git_alias(name, list(cdirs), self.cwd, self._lookup_timeout())
        return self._alias_cache[key]

    # -- text level -------------------------------------------------------

    def _analyze(self, command, depth, reasons):
        cfg = self.config
        self._tick()
        command = command.replace("\x00", "")  # see _neutralize
        if depth > cfg.max_depth:
            reasons.append("nesting too deep to analyse")
            return
        if len(command) > cfg.max_command_length:
            reasons.append(f"command longer than {cfg.max_command_length} characters")
            return
        text, bodies = _split_heredocs(command)
        for introducer, body in bodies:
            self._check_heredoc(introducer, body, depth, reasons)
        for segment, targets in _segments(_tokenize(text)):
            self._check_segment(segment, targets, depth, reasons)

    def _check_heredoc(self, introducer, body, depth, reasons):
        tokens = _tokenize(introducer)
        names = {_basename(t) for t in tokens}
        if names & self.config.shells:
            self._analyze(body, depth + 1, reasons)
        elif any(self._is_script_interpreter(n) for n in names):
            self._check_inline_code(body, reasons)

    def _is_script_interpreter(self, name):
        return any(p.search(name) for p in self.config.script_interpreters)

    # -- segment level ----------------------------------------------------

    def _check_segment(self, tokens, targets, depth, reasons):
        cfg = self.config
        self._tick()
        for target in targets:
            self._check_written_path(target, reasons, "redirect target")
        # substitutions run even in `VAR="$(...)"` and inside echo arguments
        for token in tokens:
            for body in _substitutions(token):
                self._analyze(body, depth + 1, reasons)
        index = 0
        while index < len(tokens) and (_ASSIGNMENT.match(tokens[index]) or tokens[index] in _SHELL_KEYWORDS):
            index += 1
        if index >= len(tokens):
            return
        first = tokens[index]
        base = _basename(first)
        if _has_expansion(first) and "/" not in first:
            reasons.append(f"command word '{first}' is an unresolved expansion")
            return
        if base in cfg.non_executing:
            return
        if base in cfg.refused_commands:
            reasons.append(f"'{base}' is refused (it defines commands for later in the same call)")
        if base in cfg.cwd_changing_commands:
            self._cwd_changed = True
        self._check_env(tokens, index, base, reasons)
        if base in cfg.wrappers:
            for token in tokens[index + 1:]:
                if token.startswith("--split-string="):  # env --split-string='git push ...'
                    token = token.split("=", 1)[1]
                elif token.startswith("-S") and len(token) > 2 and not token.startswith("--"):  # env -S"git push ..."
                    token = token[2:]
                if re.search(r"\s", token):
                    self._analyze(token, depth + 1, reasons)
        dynamic = False
        for i, token in enumerate(tokens):
            name = _basename(token)
            if name in cfg.path_writers or name in cfg.path_writers_with_option:
                self._check_path_writer(tokens[i + 1:], name, reasons)
            if name == "git":
                self._check_git(tokens[i + 1:], dynamic, depth, reasons)
            elif name.startswith("git-") and len(name) > 4:
                self._check_git([name[4:]] + tokens[i + 1:], dynamic, depth, reasons)
            elif name == "gh":
                self._check_gh(tokens[i + 1:], dynamic, reasons)
            elif name in cfg.http_clients:
                self._check_http(tokens[i:], reasons)
            elif name in cfg.shells:
                self._check_shell(tokens[i + 1:], depth, reasons)
            elif name == "eval":
                self._analyze(" ".join(tokens[i + 1:]), depth + 1, reasons)
            elif self._is_script_interpreter(name):
                self._check_inline_code(" ".join(tokens[i + 1:]), reasons)
            elif name in cfg.dynamic_arg:
                dynamic = True

    def _check_env(self, tokens, index, base, reasons):
        cfg = self.config
        names = [m.group(1) for m in (_ASSIGNMENT.match(t) for t in tokens[:index]) if m]
        if base in cfg.env_setting_commands:
            for token in tokens[index + 1:]:
                match = _ASSIGNMENT.match(token)
                if match:
                    names.append(match.group(1))
                elif base == "env" and not token.startswith("-"):
                    break
        for name in names:
            if any(name.upper().startswith(prefix) for prefix in cfg.env_prefixes):
                reasons.append(f"environment variable {name} is refused (it changes how git runs)")

    def _check_written_path(self, path, reasons, what):
        cfg = self.config
        candidates = {path, posixpath.normpath(path) if path else path}
        if any(p.search(c) for p in cfg.protected_paths for c in candidates):
            reasons.append(f"{what} '{path}' names the hook, its configuration or the Claude settings")

    def _check_path_writer(self, args, base, reasons):
        cfg = self.config
        if base in cfg.path_writers:
            for arg in args:
                if not arg.startswith("-"):
                    self._check_written_path(arg, reasons, f"{base} target")
        elif base in cfg.path_writers_with_option:
            options = cfg.path_writers_with_option[base]
            if any(arg == o or (o.startswith("-") and not o.startswith("--") and arg.startswith(o)) for arg in args for o in options):
                for arg in args:
                    if not arg.startswith("-"):
                        self._check_written_path(arg, reasons, f"{base} in-place target")

    def _check_shell(self, args, depth, reasons):
        index = 0
        script_given = False
        while index < len(args):
            token = args[index]
            if token in ("--version", "--help", "-h"):
                return
            if token in ("-o", "-O", "+o", "+O", "--rcfile", "--init-file"):
                index += 2
                continue
            if token.startswith("-") and not token.startswith("--") and len(token) > 1 and "c" in token[1:]:
                position = index + 1
                while position < len(args) and args[position] == "--":
                    position += 1  # sh -c -- 'command'
                if position >= len(args):
                    reasons.append("shell -c without a command")
                    return
                self._analyze(args[position], depth + 1, reasons)
                return
            if token.startswith("-") and token != "-":
                index += 1
                continue
            script_given = not (token == "-" or token.startswith("/dev/") or token.startswith("/proc/"))
            break
        if not script_given:
            reasons.append("shell reads its commands from stdin (cannot be analysed)")

    def _check_inline_code(self, code, reasons):
        cfg = self.config
        if any(m.search(code) for m in cfg.inline_markers):
            for pattern in cfg.inline_deny:
                if pattern.search(code):
                    reasons.append("inline code starts a git push / gh merge / GitHub API call")
                    return

    # -- git --------------------------------------------------------------

    def _check_git(self, args, dynamic, depth, reasons):
        cfg = self.config
        cdirs = []
        explicit_dir = False
        index = 0
        while index < len(args) and args[index].startswith("-"):
            token = args[index]
            if token in cfg.git_global_with_value:
                value = args[index + 1] if index + 1 < len(args) else ""
                if token == "-C":
                    cdirs.append(value)
                elif token in ("-c", "--config-env"):
                    self._check_c_value(value, reasons)
                elif token in ("--git-dir", "--work-tree"):
                    explicit_dir = True
                index += 2
                continue
            if token.startswith("--git-dir") or token.startswith("--work-tree"):
                explicit_dir = True
            elif token.startswith("--config-env="):
                self._check_c_value(token.split("=", 1)[1], reasons)
            elif token.startswith("-c") and len(token) > 2 and not token.startswith("--"):
                self._check_c_value(token[2:], reasons)
            index += 1
        if index >= len(args):
            return
        sub, rest = args[index], args[index + 1:]
        if _has_expansion(sub):
            reasons.append(f"git subcommand '{sub}' is an unresolved expansion")
            return
        self._check_local(sub, rest, cdirs, explicit_dir, reasons)
        self._check_sub_options(sub, rest, reasons)
        if sub == "push":
            self._check_push(rest, cdirs, explicit_dir, dynamic, reasons)
        elif sub == "add":
            self._check_add(rest, dynamic, reasons)
        elif sub == "commit":
            self._check_commit(rest, reasons)
        elif sub == "config":
            self._check_config(rest, reasons)
        elif sub not in cfg.git_known:
            self._check_alias(sub, rest, cdirs, depth, reasons)

    def _c_key_refused(self, key):
        """Every config key outside the allow-list can define aliases, run
        programs or redirect a push."""
        key = key.casefold()
        return not any(key.startswith(prefix) for prefix in self.config.git_allowed_c_prefixes)

    def _check_c_value(self, value, reasons):
        key = value.split("=", 1)[0]
        if self._c_key_refused(key):
            reasons.append(f"git -c {key.casefold()} is refused (key not in the allow-list)")

    def _check_sub_options(self, sub, rest, reasons):
        """Options and words that make a git subcommand run a program."""
        cfg = self.config
        options = cfg.git_refused_options.get(sub)
        if options:
            long_options = [o for o in options if o.startswith("--")]
            short_options = [o for o in options if not o.startswith("--")]
            for token in rest:
                if token == "--":
                    break
                if token.startswith("--"):
                    if self._long_option_refused(token.split("=", 1)[0], long_options):
                        reasons.append(f"git {sub} {token.split('=', 1)[0]} runs a program and is refused")
                elif token.startswith("-") and len(token) > 1 and any(token.startswith(o) for o in short_options):
                    reasons.append(f"git {sub} {token[:2]} runs a program and is refused")
        words = cfg.git_refused_words.get(sub)
        if words:
            first = next((t for t in rest if not t.startswith("-")), None)
            if first is not None and first.casefold() in words:
                reasons.append(f"git {sub} {first} is refused")

    def _check_config(self, args, reasons):
        read_only = any(a in ("--get", "--get-all", "--get-regexp", "--list", "-l", "--get-urlmatch") for a in args)
        if read_only:
            return
        for arg in args:
            if arg.startswith("-"):
                continue
            # the first word is the key, the rest its value (git config user.name x)
            if self._c_key_refused(arg):
                reasons.append(f"git config {arg.casefold()} is refused (key not in the allow-list)")
            break

    def _check_alias(self, name, rest, cdirs, depth, reasons):
        value = self._alias(name, cdirs)
        if not value:
            return
        if value.startswith("!"):
            self._analyze(value[1:] + " " + " ".join(shlex.quote(r) for r in rest), depth + 1, reasons)
        else:
            try:
                words = shlex.split(value)
            except ValueError as exc:
                raise GuardError(f"git alias '{name}' cannot be parsed ({exc})") from exc
            self._analyze("git " + " ".join(shlex.quote(w) for w in words + rest), depth + 1, reasons)

    def _long_option_refused(self, name, refused, extensions=False):
        """`name` is a refused long option or a unique abbreviation of one.
        With `extensions` a longer option that starts with a refused one
        (--force-with-lease for --force) is refused as well; git add/commit
        have real options such as --allow-empty that must stay allowed."""
        if len(name) < self.config.min_abbreviation:
            return False
        return any(r.startswith(name) or (extensions and name.startswith(r)) for r in refused)

    # -- local changes to a protected branch ----------------------------------

    def _check_local(self, sub, rest, cdirs, explicit_dir, reasons):
        cfg = self.config
        if sub in cfg.local_refused:
            reasons.append(f"git {sub} is refused")
        if sub in cfg.local_on_protected:
            self._check_current_branch(cdirs, explicit_dir, reasons, f"git {sub}")
        if sub == "pull" and cfg.local_pull_option not in rest:
            self._check_current_branch(cdirs, explicit_dir, reasons, f"git pull without {cfg.local_pull_option}")
        names_branch = any(not a.startswith("-") and self._is_protected(a) for a in rest)
        if sub in cfg.local_naming_subcommands and names_branch:
            reasons.append(f"git {sub} on a protected branch is refused")
        options = cfg.local_options_naming.get(sub)
        if options and names_branch:
            for token in rest:
                if token.startswith("--"):
                    hit = token.split("=", 1)[0] in options
                elif token.startswith("-") and len(token) > 1:
                    hit = token in options or any(f"-{ch}" in options for ch in token[1:])
                else:
                    hit = False
                if hit:
                    reasons.append(f"git {sub} {token} on a protected branch is refused")
        if sub == "fetch":
            for token in rest:
                if not (token.startswith("+") and ":" in token):
                    continue
                destination = token.split(":", 1)[1]
                if self._is_protected(destination) or any(ch in destination for ch in "*?["):
                    reasons.append(f"forced fetch refspec '{token}' would overwrite a protected branch")
        if sub in cfg.local_switching:
            self._note_branch_switch(sub, rest)

    def _note_branch_switch(self, sub, rest):
        """Remember that this command may change the current branch, so a
        later `git commit` / `git push` without a refspec in the same call
        is not judged by the branch HEAD points to when the hook runs."""
        if sub in ("checkout", "switch"):
            if "--" in rest:
                return  # restoring files, not switching
            creating = None
            for i, token in enumerate(rest):
                if token in ("-b", "-B", "-c", "-C", "--orphan", "--create", "--force-create") and i + 1 < len(rest):
                    creating = rest[i + 1]
                    break
            if creating is not None:
                if self._is_protected(creating) or _has_expansion(creating):
                    self._branch_switched = True
                else:
                    self._branch_override = creating  # a new branch: known, not protected
                return
            if any(t == "-" or not t.startswith("-") for t in rest):  # `git checkout -`: the previous branch
                self._branch_switched = True  # name of an existing branch (or a path): unknown
            return
        self._branch_switched = True  # symbolic-ref, worktree: may move HEAD

    # -- git push ---------------------------------------------------------

    def _check_push(self, args, cdirs, explicit_dir, dynamic, reasons):
        cfg = self.config
        if dynamic:
            reasons.append("git push behind xargs/parallel cannot be checked")
        positionals = []
        repo_option = False
        index = 0
        options_done = False
        while index < len(args):
            token = args[index]
            if options_done or not token.startswith("-") or token == "-":
                positionals.append(token)
                index += 1
                continue
            if token == "--":
                options_done = True
                index += 1
                continue
            if token.startswith("--"):
                name = token.split("=", 1)[0]
                if self._long_option_refused(name, cfg.push_long, extensions=True):
                    reasons.append(f"git push option {name} is refused")
                if name == "--repo":
                    repo_option = True
                if name in cfg.push_with_value and "=" not in token:
                    index += 2
                    continue
            else:
                for letter in token[1:]:
                    if letter in cfg.push_short:
                        reasons.append(f"git push option -{letter} is refused")
                    if f"-{letter}" in cfg.push_with_value:
                        break
                if token in cfg.push_with_value:
                    index += 2
                    continue
            index += 1
        if positionals and not repo_option and _has_expansion(positionals[0]):
            # `git push $(git remote) master`: the substitution shifts the positions
            reasons.append(f"git push remote '{positionals[0]}' is an unresolved expansion")
        refspecs = positionals if repo_option else positionals[1:]
        for refspec in refspecs:
            self._check_refspec(refspec, cdirs, explicit_dir, reasons)
        if not refspecs:
            self._check_current_branch(cdirs, explicit_dir, reasons, "git push without a refspec")

    def _check_current_branch(self, cdirs, explicit_dir, reasons, what):
        if self._branch_switched:
            reasons.append(f"{what}: the branch may have been switched earlier in the same command (give an explicit refspec)")
            return
        if self._cwd_changed:
            reasons.append(f"{what}: the working directory changed earlier in the same command, the branch is unknown")
            return
        if self._branch_override is not None:
            return  # a branch created earlier in the same command (checked: not protected)
        if explicit_dir:
            reasons.append(f"{what} with --git-dir/--work-tree: current branch unknown")
            return
        branch = self._branch(cdirs)
        if branch and self._is_protected(branch):
            reasons.append(f"{what} on the protected branch '{branch}'")

    def _normalize_ref(self, ref):
        ref = ref.strip().rstrip("/")
        for prefix in ("refs/heads/", "heads/", "refs/"):
            if ref.startswith(prefix):
                ref = ref[len(prefix):]
                break
        return ref

    def _is_protected(self, ref):
        return self._normalize_ref(ref).casefold() in self.config.protected

    def _check_refspec(self, refspec, cdirs, explicit_dir, reasons):
        cfg = self.config
        if _has_expansion(refspec):
            # checked before splitting: ${B:-master} contains a colon
            reasons.append(f"refspec '{refspec}' contains an unresolved expansion")
            return
        if refspec in cfg.push_words:
            reasons.append(f"git push {refspec} <name> (tag push) is refused")
        spec = refspec
        if spec.startswith("+"):
            reasons.append(f"forced refspec '{refspec}' is refused")
            spec = spec[1:]
        if ":" in spec:
            source, destination = spec.split(":", 1)
            if not source:
                reasons.append(f"deleting refspec '{refspec}' is refused")
        else:
            source, destination = spec, spec
        if destination in ("HEAD", "@") and ":" not in spec:
            self._check_current_branch(cdirs, explicit_dir, reasons, f"git push {refspec}")
            return
        if not destination:
            return
        if any(ch in destination for ch in "*?[{~^"):
            reasons.append(f"refspec destination '{destination}' is a pattern")
            return
        if self._is_protected(destination):
            reasons.append(f"push to protected branch '{destination}'")
        if destination.startswith("refs/") and not any(destination.startswith(p) for p in cfg.push_dst_allowed):
            reasons.append(f"push to '{destination}' (outside refs/heads) is refused")
        if any(destination.startswith(p) for p in cfg.push_dst_refused):
            reasons.append(f"tag push '{destination}' is refused")

    # -- git add / commit -------------------------------------------------

    def _stages_everything(self, token):
        norm = posixpath.normpath(token) if token else ""
        if norm in (".", "..") or norm.startswith("../"):
            return True
        if posixpath.isabs(norm):
            if norm == "/":
                return True
            if self.cwd:
                cwd = posixpath.normpath(self.cwd)
                return cwd == norm or cwd.startswith(norm.rstrip("/") + "/")
        return False

    def _check_pathspecs(self, paths, reasons, what):
        cfg = self.config
        for token in paths:
            if _has_expansion(token):
                reasons.append(f"{what} pathspec '{token}' is an unresolved expansion")
            elif any(p.search(token) for p in cfg.pathspec_refused) or self._stages_everything(token):
                reasons.append(f"{what} pathspec '{token}' stages everything or a pattern")
            elif any(p.search(token) for p in cfg.pathspec_sensitive):
                reasons.append(f"{what} pathspec '{token}' names a sensitive path")

    def _check_add(self, args, dynamic, reasons):
        cfg = self.config
        if dynamic:
            reasons.append("git add behind xargs/parallel stages paths that cannot be checked")
        options_done = False
        paths = []
        for token in args:
            if not options_done and token == "--":
                options_done = True
                continue
            if not options_done and token.startswith("--"):
                if self._long_option_refused(token.split("=", 1)[0], cfg.add_long):
                    reasons.append(f"git add option {token} is refused")
                continue
            if not options_done and token.startswith("-") and len(token) > 1:
                for letter in token[1:]:
                    if letter in cfg.add_short:
                        reasons.append(f"git add option -{letter} is refused (stage targeted paths only)")
                continue
            paths.append(token)
        self._check_pathspecs(paths, reasons, "git add")

    def _check_commit(self, args, reasons):
        cfg = self.config
        index = 0
        options_done = False
        paths = []
        while index < len(args):
            token = args[index]
            if not options_done and token == "--":
                options_done = True
                index += 1
                continue
            if not options_done and token.startswith("--"):
                name = token.split("=", 1)[0]
                if self._long_option_refused(name, cfg.commit_long):
                    reasons.append(f"git commit {name} stages all tracked changes")
                if name in cfg.commit_with_value and "=" not in token:
                    index += 2
                    continue
            elif not options_done and token.startswith("-") and len(token) > 1:
                consumes_next = False
                for position, letter in enumerate(token[1:], start=1):
                    if letter in cfg.commit_short:
                        reasons.append(f"git commit -{letter} stages all tracked changes")
                    if letter in cfg.commit_value_letters:
                        consumes_next = position == len(token) - 1
                        break
                if token in cfg.commit_with_value or consumes_next:
                    index += 2
                    continue
            else:
                paths.append(token)
            index += 1
        self._check_pathspecs(paths, reasons, "git commit")

    # -- gh / http --------------------------------------------------------

    def _check_gh(self, args, dynamic, reasons):
        cfg = self.config
        words = []
        index = 0
        while index < len(args) and len(words) < 2:
            token = args[index]
            if token in cfg.gh_global_with_value:
                index += 2
                continue
            if token.startswith("-"):
                index += 1
                continue
            words.append(token.casefold())
            index += 1
        if len(words) == 2 and tuple(words) in cfg.gh_refused:
            reasons.append(f"gh {words[0]} {words[1]} is refused")
        if dynamic and len(words) == 2 and words[0] == "pr":
            reasons.append("gh pr behind xargs/parallel cannot be checked")
        if words and words[0] == "api":
            self._check_gh_api(args, reasons)

    def _check_gh_api(self, args, reasons):
        cfg = self.config
        method = None
        implicit_write = False
        header_write = False
        for i, token in enumerate(args):
            if token in ("-X", "--method"):
                method = args[i + 1].upper() if i + 1 < len(args) else ""
            elif token.startswith("--method="):
                method = token.split("=", 1)[1].upper()
            elif token.startswith("-X") and len(token) > 2 and not token.startswith("--"):
                method = token[2:].upper()
            for option in cfg.api_field_options:
                if token == option or (token.startswith(option) and not option.startswith("--") and len(token) > len(option)) \
                        or token.startswith(option + "="):
                    implicit_write = True
            header = None
            if token in ("-H", "--header") and i + 1 < len(args):
                header = args[i + 1]
            elif token.startswith("--header="):
                header = token.split("=", 1)[1]
            elif token.startswith("-H") and len(token) > 2 and not token.startswith("--"):
                header = token[2:]
            if header is not None and any(p.search(header) for p in cfg.api_header_patterns):
                header_write = True  # X-HTTP-Method-Override: PUT
        if method in cfg.api_read_methods and not header_write:
            return  # an explicit GET stays a read even with fields
        if any(_has_expansion(t) for t in args):
            # `gh api -X $M ...`, `gh api $(echo -X PUT) ...`: the method is unknown
            reasons.append("gh api call with an unresolved expansion (the request method is unknown)")
            return
        write = header_write or ((method in cfg.api_write_methods) if method else implicit_write)
        if not write:
            return
        if cfg.api_refuse_all_writes:
            reasons.append("gh api write requests are refused (use gh issue comment / gh pr create)")
            return
        for token in args:
            decoded = unquote(token)  # mast%65r
            if cfg.protected_word.search(decoded) or any(p.search(decoded) for p in cfg.api_refused_args):
                reasons.append("gh api write request touches a merge, a protected branch or an uninspectable body")
                return

    def _check_http(self, tokens, reasons):
        cfg = self.config
        for token in tokens[1:]:
            for option in cfg.http_refused_options:
                if token == option or token.startswith(option + "=") or (not option.startswith("--") and token.startswith(option) and len(token) > len(option)):
                    reasons.append(f"{tokens[0]} {option} reads options from a file (cannot be analysed)")
                    return
        args = tokens[1:]
        text = " ".join(args).casefold()
        # any unresolved word may be the URL, the method or an option
        host_unknown = any(_has_expansion(t) for t in args if not t.startswith("-"))
        if not (host_unknown or any(host in text for host in cfg.http_hosts)):
            return
        write, method = self._http_is_write(_basename(tokens[0]), args)
        explicit_read = method is not None and method.upper() in cfg.http_read_methods and not write
        if write or (host_unknown and not explicit_read):
            reasons.append("write request (or unresolved expansion) to a GitHub or unknown host outside gh is refused")

    def _http_is_write(self, client, args):
        """(write?, method) of a curl/wget/HTTPie request: sends data or uses
        a write method (long options, short clusters such as -sXPUT and -sd,
        HTTPie words)."""
        cfg = self.config
        method = None
        write = False
        for i, token in enumerate(args):
            if token.startswith("--"):
                name = token.split("=", 1)[0]
                if name in cfg.http_write_long:
                    if name in ("--request", "--method"):
                        method = token.split("=", 1)[1] if "=" in token else (args[i + 1] if i + 1 < len(args) else "")
                    else:
                        write = True
            elif token.startswith("-") and len(token) > 1:
                for position, letter in enumerate(token[1:], start=1):
                    if letter in cfg.http_method_letters:
                        method = token[position + 1:] or (args[i + 1] if i + 1 < len(args) else "")
                        break
                    if letter in cfg.http_write_short:
                        write = True
            elif client in cfg.httpie_clients:
                if token in cfg.api_write_methods or any(p.search(token) for p in cfg.httpie_write_patterns):
                    write = True
        if method is not None and method.upper() not in cfg.http_read_methods:
            write = True
        return write, method


# --------------------------------------------------------------------------
# hook entry point
# --------------------------------------------------------------------------

def evaluate(payload, config, branch_resolver=None, alias_resolver=None, clock=time.monotonic):
    """(allowed, reasons) for one hook payload (the JSON object from stdin)."""
    if not isinstance(payload, dict):
        return False, ["hook input is not a JSON object"]
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return False, ["hook input has no tool_name"]
    if tool_name.casefold() != "bash":
        return True, []
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return False, ["Bash call without a command string"]
    guard = Guard(config, cwd=payload.get("cwd") or None, branch_resolver=branch_resolver, alias_resolver=alias_resolver, clock=clock)
    reasons = guard.check(command)
    return not reasons, reasons


def main(stdin=None, stderr=None, config_path=DEFAULT_CONFIG_PATH):
    stdin = stdin or sys.stdin
    stderr = stderr or sys.stderr
    try:
        payload = json.load(stdin)
        config = load_config(config_path)
        allowed, reasons = evaluate(payload, config)
    except Exception as exc:  # fail closed on anything unexpected
        stderr.write(f"bash_guard: REFUSED (internal error, fail closed): {type(exc).__name__}: {exc}\n")
        return EXIT_BLOCK
    if allowed:
        return EXIT_ALLOW
    stderr.write("bash_guard: REFUSED by config/bash_guard.yaml rules (docs/bash-guard.md): " + "; ".join(reasons) + "\n")
    return EXIT_BLOCK


if __name__ == "__main__":
    sys.exit(main())

"""PreToolUse hook for the Bash tool (Issue #60, Stufe 2, docs/bash-guard.md).

Claude Code runs this script before every Bash call, in every permission
mode (default, auto, bypassPermissions): the command arrives as JSON on
stdin ({"tool_name": "Bash", "tool_input": {"command": ...}, "cwd": ...}).
Exit code 2 blocks the call and shows stderr to the model; exit code 0 lets
the normal permission flow continue.

The hook refuses commands that would push to / merge into / delete a
protected branch (config/bash_guard.yaml: master, main) in any spelling it
can resolve, plus the "stage everything" forms of git add and gh pr merge /
gh api merge calls. It parses the command with a shell tokenizer instead of
matching text, so quoting (ma''ster), wrappers (git -C ., bash -c, env,
timeout, xargs), refspecs (HEAD:master, +ref, :ref), option abbreviations
(--forc) and nested strings are handled. What it cannot see (script files,
pytest, obfuscated code) is listed in docs/bash-guard.md.

Fail closed: an unreadable config, invalid input, a parse error or any
internal error refuses the call (exit 2). Patterns and limits live in
config/bash_guard.yaml, none in this file.
"""

import json
import posixpath
import re
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

EXIT_ALLOW = 0
EXIT_BLOCK = 2
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "bash_guard.yaml"

_OPERATOR_CHARS = frozenset(";&|()<>")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
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
    if not isinstance(value, kind) or isinstance(value, bool) and kind is int:
        raise GuardConfigError(f"{where}.{key}: expected {kind.__name__}, got {type(value).__name__}")
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
        self.non_executing = set(_str_list(data, "non_executing_commands", "config"))
        self.dynamic_arg = set(_str_list(data, "dynamic_arg_commands", "config"))
        self.shells = set(_str_list(data, "shell_interpreters", "config"))
        self.script_interpreters = _regex_list(data, "script_interpreter_patterns", "config")
        self.inline_markers = _regex_list(data, "inline_exec_markers", "config")
        self.inline_deny = _regex_list(data, "inline_deny_patterns", "config")

        git = _require(data, "git", dict, "config")
        self.git_global_with_value = set(_str_list(git, "global_options_with_value", "git"))
        self.git_c_prefixes = [p.casefold() for p in _str_list(git, "refused_c_key_prefixes", "git")]
        self.git_known = set(_str_list(git, "known_subcommands", "git"))
        push = _require(git, "push", dict, "git")
        self.push_long = _str_list(push, "refused_long_options", "git.push")
        self.push_short = set(_str_list(push, "refused_short_letters", "git.push"))
        self.push_with_value = set(_str_list(push, "options_with_value", "git.push"))
        self.push_dst_prefixes = _str_list(push, "refused_destination_prefixes", "git.push")
        add = _require(git, "add", dict, "git")
        self.add_long = _str_list(add, "refused_long_options", "git.add")
        self.add_short = set(_str_list(add, "refused_short_letters", "git.add"))
        self.add_pathspec_refused = _regex_list(add, "refused_pathspec_patterns", "git.add")
        self.add_pathspec_sensitive = _regex_list(add, "sensitive_pathspec_patterns", "git.add")
        commit = _require(git, "commit", dict, "git")
        self.commit_short = set(_str_list(commit, "refused_short_letters", "git.commit"))
        self.commit_long = set(_str_list(commit, "refused_long_options", "git.commit"))
        self.commit_with_value = set(_str_list(commit, "options_with_value", "git.commit"))
        self.commit_short_value_letters = set(_str_list(commit, "short_letters_with_value", "git.commit"))

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
        self.api_refused_args = _regex_list(api, "refused_argument_patterns", "gh.api")

        http = _require(data, "http", dict, "config")
        self.http_clients = set(_str_list(http, "clients", "http"))
        self.http_hosts = [h.casefold() for h in _str_list(http, "github_hosts", "http")]
        self.http_write_markers = _str_list(http, "write_markers", "http")


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

def _git_output(cdirs, cwd, args):
    command = ["git"]
    for directory in cdirs:
        command += ["-C", directory]
    command += list(args)
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GuardError(f"git lookup failed: {exc}") from exc
    return result


def current_branch(cdirs, cwd):
    """Branch HEAD points to, or None on a detached HEAD / outside a repo."""
    result = _git_output(cdirs, cwd, ["symbolic-ref", "--short", "-q", "HEAD"])
    if result.returncode == 0:
        return result.stdout.strip() or None
    if result.returncode in (1, 128):
        return None
    raise GuardError(f"git symbolic-ref failed with exit {result.returncode}")


def git_alias(name, cdirs, cwd):
    """Value of git config alias.<name>, or None."""
    result = _git_output(cdirs, cwd, ["config", "--get", f"alias.{name}"])
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
    if pending:
        # heredoc opened but never closed: the rest was already kept in `out`
        pass
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
    i = 0
    while i < len(text):
        ch = text[i]
        if in_single:
            # `$(` is literal text in single quotes: break the pair so the
            # substitution extraction does not see it (_analyze removes the
            # marker again when the string is re-analysed as a script, e.g.
            # the argument of bash -c, where it IS code)
            out.append("$\x00" if ch == "$" and text[i + 1:i + 2] == "(" else ch)
            in_single = ch != "'"
        elif ch == "\\" and i + 1 < len(text):
            out.append(ch)
            out.append(text[i + 1] if text[i + 1] != "\n" else " ; ")
            i += 1
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
    text = re.sub(r"\\\n", " ", text)
    text = _neutralize(text.replace("\r", " "))
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError as exc:
        raise GuardError(f"command cannot be parsed ({exc})") from exc


def _segments(tokens):
    segment = []
    for token in tokens:
        if token and all(ch in _OPERATOR_CHARS for ch in token):
            if segment:
                yield segment
            segment = []
        else:
            segment.append(token)
    if segment:
        yield segment


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
    def __init__(self, config, cwd=None, branch_resolver=None, alias_resolver=None):
        self.config = config
        self.cwd = cwd
        self._branch = branch_resolver or (lambda cdirs: current_branch(cdirs, self.cwd))
        self._alias = alias_resolver or (lambda name, cdirs: git_alias(name, cdirs, self.cwd))

    def check(self, command):
        """List of reasons the command is refused (empty = allowed)."""
        reasons = []
        try:
            self._analyze(command, 0, reasons)
        except GuardError as exc:
            reasons.append(str(exc))
        return reasons

    # -- text level -------------------------------------------------------

    def _analyze(self, command, depth, reasons):
        cfg = self.config
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
        for segment in _segments(_tokenize(text)):
            self._check_segment(segment, depth, reasons)

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

    def _check_segment(self, tokens, depth, reasons):
        cfg = self.config
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
        if _has_expansion(first) and "/" not in first:
            reasons.append(f"command word '{first}' is an unresolved expansion")
            return
        if _basename(first) in cfg.non_executing:
            return
        dynamic = False
        for i, token in enumerate(tokens):
            name = _basename(token)
            if name == "git":
                self._check_git(tokens[i + 1:], dynamic, depth, reasons)
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
                if index + 1 >= len(args):
                    reasons.append("shell -c without a command")
                    return
                self._analyze(args[index + 1], depth + 1, reasons)
                return
            if token.startswith("-"):
                index += 1
                continue
            script_given = True
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
                elif token == "-c":
                    self._check_c_value(value, reasons)
                elif token in ("--git-dir", "--work-tree"):
                    explicit_dir = True
                index += 2
                continue
            if token.startswith("--git-dir") or token.startswith("--work-tree"):
                explicit_dir = True
            elif token.startswith("-c") and len(token) > 2 and not token.startswith("--"):
                self._check_c_value(token[2:], reasons)
            index += 1
        if index >= len(args):
            return
        sub, rest = args[index], args[index + 1:]
        if _has_expansion(sub):
            reasons.append(f"git subcommand '{sub}' is an unresolved expansion")
        elif sub == "push":
            self._check_push(rest, cdirs, explicit_dir, dynamic, reasons)
        elif sub == "add":
            self._check_add(rest, reasons)
        elif sub == "commit":
            self._check_commit(rest, reasons)
        elif sub == "config":
            if any(a.casefold().startswith("alias.") for a in rest):
                reasons.append("git config alias.* is refused")
        elif sub not in cfg.git_known:
            self._check_alias(sub, rest, cdirs, depth, reasons)

    def _check_c_value(self, value, reasons):
        key = value.split("=", 1)[0].casefold()
        if any(key.startswith(prefix) for prefix in self.config.git_c_prefixes):
            reasons.append(f"git -c {key} is refused")

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

    def _long_option_refused(self, name, refused):
        if len(name) < 3:
            return False
        return any(r.startswith(name) or name.startswith(r) for r in refused)

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
                if self._long_option_refused(name, cfg.push_long):
                    reasons.append(f"git push option {name} is refused")
                if name == "--repo":
                    repo_option = True
                if name in cfg.push_with_value and "=" not in token:
                    index += 2
                    continue
            else:
                letters = token[1:]
                for letter in letters:
                    if letter in cfg.push_short:
                        reasons.append(f"git push option -{letter} is refused")
                    if f"-{letter}" in cfg.push_with_value:
                        break
                if token in cfg.push_with_value:
                    index += 2
                    continue
            index += 1
        refspecs = positionals if repo_option else positionals[1:]
        implicit = not refspecs
        for refspec in refspecs:
            self._check_refspec(refspec, cdirs, explicit_dir, reasons)
        if implicit:
            self._check_current_branch(cdirs, explicit_dir, reasons, "git push without a refspec")

    def _check_current_branch(self, cdirs, explicit_dir, reasons, what):
        if explicit_dir:
            reasons.append(f"{what} with --git-dir/--work-tree: current branch unknown")
            return
        branch = self._branch(cdirs)
        if branch and self._is_protected(branch):
            reasons.append(f"{what} would push the current branch '{branch}'")

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
        if _has_expansion(destination) or any(ch in destination for ch in "*?[{~^"):
            reasons.append(f"refspec destination '{destination}' is a pattern or unresolved expansion")
            return
        if self._is_protected(destination):
            reasons.append(f"push to protected branch '{destination}'")
        if any(destination.startswith(p) for p in cfg.push_dst_prefixes):
            reasons.append(f"tag push '{destination}' is refused")

    # -- git add / commit -------------------------------------------------

    def _check_add(self, args, reasons):
        cfg = self.config
        options_done = False
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
            if _has_expansion(token):
                reasons.append(f"git add pathspec '{token}' is an unresolved expansion")
            elif any(p.search(token) for p in cfg.add_pathspec_refused):
                reasons.append(f"git add pathspec '{token}' stages everything or a pattern")
            elif any(p.search(token) for p in cfg.add_pathspec_sensitive):
                reasons.append(f"git add pathspec '{token}' names a sensitive path")

    def _check_commit(self, args, reasons):
        cfg = self.config
        index = 0
        while index < len(args):
            token = args[index]
            if token == "--":
                return
            if token.startswith("--"):
                name = token.split("=", 1)[0]
                if name in cfg.commit_long:
                    reasons.append(f"git commit {name} stages all tracked changes")
                if name in cfg.commit_with_value and "=" not in token:
                    index += 2
                    continue
            elif token.startswith("-") and len(token) > 1:
                for letter in token[1:]:
                    if letter in cfg.commit_short:
                        reasons.append(f"git commit -{letter} stages all tracked changes")
                    if letter in cfg.commit_short_value_letters:
                        break
                if token in cfg.commit_with_value:
                    index += 2
                    continue
            index += 1

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
        write = (method in cfg.api_write_methods) if method else implicit_write
        if not write:
            return
        for token in args:
            if cfg.protected_word.search(token) or any(p.search(token) for p in cfg.api_refused_args):
                reasons.append("gh api write request touches a merge, a protected branch or an uninspectable body")
                return

    def _check_http(self, tokens, reasons):
        cfg = self.config
        text = " ".join(tokens).casefold()
        if not any(host in text for host in cfg.http_hosts):
            return
        for token in tokens[1:]:
            for marker in cfg.http_write_markers:
                if token == marker or token.startswith(marker + "=") or (
                    not marker.startswith("--") and token.startswith(marker) and len(token) > len(marker)
                ):
                    reasons.append("write request to the GitHub API outside gh is refused")
                    return


# --------------------------------------------------------------------------
# hook entry point
# --------------------------------------------------------------------------

def evaluate(payload, config, branch_resolver=None, alias_resolver=None):
    """(allowed, reasons) for one hook payload (the JSON object from stdin)."""
    if not isinstance(payload, dict):
        return False, ["hook input is not a JSON object"]
    if payload.get("tool_name") != "Bash":
        return True, []
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return False, ["Bash call without a command string"]
    guard = Guard(config, cwd=payload.get("cwd") or None, branch_resolver=branch_resolver, alias_resolver=alias_resolver)
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

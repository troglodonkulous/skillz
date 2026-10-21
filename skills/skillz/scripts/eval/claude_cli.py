"""The only code that builds a `claude -p` argv, runs it, and reads its stream.

Every CLI-calling script (run_tasks, run_eval, run_loop, improve_description)
goes through this module, so the guardrails in spec 4.4 and the invocation
contract in 4.5 hold in one place. Import by bare name after putting
scripts/eval on sys.path.

Public API
----------
Argv construction (spec 4.5)
    build_argv(kind, prompt, *, model=None, tools=None, permission_mode=None,
               permission_prompts=None, harness_prompt=None, extra_args=())
        -> ClaudeCall
        kind is TRIGGER, EXECUTION or REWRITE. Layout:
        `claude -p <prompt> <reserved flags> [--model] --tools --permission-mode
        [--permission-prompts] [--append-system-prompt] <extra_args verbatim>`.
        The prompt is argv[2], byte-identical to the input. Raises
        PassthroughError before anything runs when extra_args holds a
        reserved, incompatible or first-class flag.
        ClaudeCall fields: argv, extra_args, additions (what this module
        added, e.g. "--tools: appended Skill"), warnings (e.g. bypassPermissions).
        ClaudeCall.record() -> dict for run.json / results files.
    validate_passthrough(extra_args) -> None   (raises PassthroughError)
    PassthroughError(ValueError)

Temp projects (spec 4.5, last paragraphs)
    make_project() -> Path        skillz-run-* dir in tempfile.gettempdir()
                                  with a `.skillz-pid` marker
    stage_skill(project, name, *, source_dir=None, skill_md=None) -> Path
                                  copy a skill dir and/or write SKILL.md text
                                  to <project>/.claude/skills/<name>/
    temp_project(...)             context manager: make_project + optional
                                  stage_skill + cleanup on exit/exception
    remove_project(path)
    sweep_stale() -> list[str]    remove skillz-run-* dirs whose marker pid is dead
    install_signal_handlers()     idempotent; also called by make_project when
                                  on the main thread. SIGINT/SIGTERM kill this
                                  process's own claude children, remove its
                                  projects, then re-deliver the signal.

Runner
    run_claude(call, cwd, *, timeout, stream_path=None, budget=None) -> RawRun
        Resolves `claude` via shutil.which at call time (ClaudeNotFound if
        absent). Thread-safe. RawRun: returncode, timed_out, stdout (str),
        stderr_tail, duration_s. With a budget, the cap check and the spawn
        happen under the budget's lock, so no call starts after the cap was
        crossed (BudgetExceeded otherwise). After a SIGINT/SIGTERM it raises
        ShuttingDown instead of spawning.
    run_and_parse(call, cwd, *, timeout, skill_name=None, skill_path=None,
                  stream_path=None, budget=None) -> RunOutcome
        run_claude + parse_stream + detect_trigger + classify. Adds the run's
        total_cost_usd to budget. skill_path defaults to
        <cwd>/.claude/skills/<skill_name>, the staged candidate.
        RunOutcome: status ("triggered" | "not_triggered" | "ok" | "error"),
        error (reason string or None), raw, summary, call.
    cli_version() -> str | None

Stream parsing
    parse_stream(text_or_lines) -> StreamSummary
        Tolerates hook events before init and non-JSON lines. Fields: init
        (dict), skills, model, tools, slash_commands, claude_code_version,
        result (dict or None), is_error, subtype, terminal_reason,
        total_cost_usd, duration_ms, num_turns, usage, total_tokens (sum of
        the four usage counters), denials, tool_uses, bad_lines.
    detect_trigger(summary, skill_name, skill_path=None) -> bool
        Exact match only: Skill tool_use with input.skill == name or
        "<prefix>:<name>" (from `assistant` events or from partial
        content_block_start + input_json_delta), or a Read of
        <skill_path>/SKILL.md (resolved paths; without skill_path no Read
        counts, so an identically named skill from --add-dir cannot score).
        Never substring-matches.
    classify(kind, returncode, timed_out, summary, triggered=None)
        -> (status, error_reason)
        Error on non-zero exit, is_error, any permission denial, timeout, or
        a missing result event. Never looks at result.subtype.

Budget
    Budget(max_total_usd=None): add(cost), may_launch(), spent, exceeded,
    launch() (context manager; raises BudgetExceeded at/over the cap).
    Thread-safe. may_launch() is False once spent >= max_total_usd. Runs in
    flight when the cap is crossed finish and are recorded.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

TRIGGER = "trigger"
EXECUTION = "execution"
REWRITE = "rewrite"
KINDS = (TRIGGER, EXECUTION, REWRITE)

PROJECT_PREFIX = "skillz-run-"
PID_MARKER = ".skillz-pid"
STDERR_TAIL_CHARS = 2000

# Execution default: Skill plus the file tools a task needs. Bash is left out
# on purpose; under --permission-prompts none it would only be denied.
EXECUTION_DEFAULT_TOOLS = "Skill,Read,Write,Edit,Glob,Grep"

# argv[1:] after `-p <prompt>`; each is set on every call.
_RESERVED_FLAGS = [
    "--output-format", "stream-json",
    "--verbose",
    "--include-partial-messages",
    "--no-session-persistence",
    "--setting-sources", "project",
    "--strict-mcp-config",
]

_RESERVED_REASON = "stream parsing and isolation depend on it, so the module sets it on every call"
_RESERVED = {
    "-p": _RESERVED_REASON, "--print": _RESERVED_REASON,
    "--output-format": _RESERVED_REASON, "--input-format": _RESERVED_REASON,
    "--verbose": _RESERVED_REASON, "--include-partial-messages": _RESERVED_REASON,
    "--setting-sources": _RESERVED_REASON, "--no-session-persistence": _RESERVED_REASON,
    "--strict-mcp-config": _RESERVED_REASON,
}
_LEAVES_SESSION = "it leaves the one-shot isolated session or never returns"
_HIDES_SKILL = "it hides the skill under test"
_INCOMPATIBLE = {
    "-c": _LEAVES_SESSION, "--continue": _LEAVES_SESSION,
    "-r": _LEAVES_SESSION, "--resume": _LEAVES_SESSION,
    "--fork-session": _LEAVES_SESSION,
    "--bg": _LEAVES_SESSION, "--background": _LEAVES_SESSION,
    "-w": _LEAVES_SESSION, "--worktree": _LEAVES_SESSION,
    "--tmux": _LEAVES_SESSION, "--cloud": _LEAVES_SESSION,
    "--teleport": _LEAVES_SESSION, "--desktop": _LEAVES_SESSION,
    "--remote-control": _LEAVES_SESSION, "--from-pr": _LEAVES_SESSION,
    "--environment": _LEAVES_SESSION,
    "--bare": "it has no subscription OAuth, no Skill tool and no skill listing, so it cannot measure auto-triggering",
    "--safe-mode": _HIDES_SKILL, "--disable-slash-commands": _HIDES_SKILL,
}
_FIRST_CLASS = {
    "--model": "the script's --model option",
    "--tools": "the script's --tools option",
    "--permission-mode": "the script's --permission-mode option",
    "--permission-prompts": "the script's --permission-prompts option",
}
_APPEND_PROMPT = "--append-system-prompt"
_BYPASS_FLAGS = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions")


class PassthroughError(ValueError):
    """A passthrough flag the module refuses; the message names flag and reason."""


class ClaudeNotFound(RuntimeError):
    """`claude` is not on PATH."""


class BudgetExceeded(RuntimeError):
    """The --max-total-usd cap was crossed; no new call is launched."""


class ShuttingDown(RuntimeError):
    """SIGINT/SIGTERM was received; no new call is launched."""


# --------------------------------------------------------------------------
# Argv construction
# --------------------------------------------------------------------------

_SHORT_FLAGS = [f for f in {**_RESERVED, **_INCOMPATIBLE} if len(f) == 2]


def _flag_of(token: str) -> str:
    """`--flag=value` -> `--flag`; `-r<id>` / `-w=name` / `-pc` -> the owned
    short flag; anything else unchanged."""
    if token.startswith("--"):
        return token.split("=", 1)[0]
    if token.startswith("-") and len(token) > 2 and token[:2] in _SHORT_FLAGS:
        return token[:2]
    return token


def add_dir_values(extra_args: Iterable[str]) -> list[str]:
    """Directories named by --add-dir in passthrough (variadic until the next flag)."""
    out: list[str] = []
    tokens = list(extra_args)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--add-dir="):
            out.append(tok.split("=", 1)[1])
        elif tok == "--add-dir":
            i += 1
            while i < len(tokens) and not tokens[i].startswith("-"):
                out.append(tokens[i])
                i += 1
            continue
        i += 1
    return out


def _scan_passthrough(extra_args: Iterable[str]) -> tuple[list[str], list[str], list[str]]:
    """Validate passthrough. Returns (args without --append-system-prompt,
    append texts in order, warnings)."""
    kept: list[str] = []
    appended: list[str] = []
    warnings: list[str] = []
    tokens = list(extra_args)
    # Every call runs in a temp project, so a relative directory would name a
    # different place than the caller meant, one that guards cannot check.
    for d in add_dir_values(tokens):
        if not Path(d).is_absolute():
            raise PassthroughError(f"--add-dir {d}: use an absolute path; calls run in a temp project")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        flag = _flag_of(tok)
        if flag in _RESERVED:
            raise PassthroughError(f"{flag} is reserved: {_RESERVED[flag]}")
        if flag in _INCOMPATIBLE:
            raise PassthroughError(f"{flag} is not allowed: {_INCOMPATIBLE[flag]}")
        if flag in _FIRST_CLASS:
            raise PassthroughError(f"{flag} cannot be passed through; use {_FIRST_CLASS[flag]} instead")
        if flag == _APPEND_PROMPT:
            if tok != flag:  # --append-system-prompt=text
                appended.append(tok.split("=", 1)[1])
                i += 1
            else:
                if i + 1 >= len(tokens):
                    raise PassthroughError(f"{flag} needs a value")
                appended.append(tokens[i + 1])
                i += 2
            continue
        if flag in _BYPASS_FLAGS:
            warnings.append(f"{flag} given in passthrough: permission checks are bypassed")
        kept.append(tok)
        i += 1
    return kept, appended, warnings


def validate_passthrough(extra_args: Iterable[str]) -> None:
    """Raise PassthroughError if extra_args holds a flag the module owns."""
    _scan_passthrough(extra_args)


@dataclass
class ClaudeCall:
    kind: str
    argv: list[str]
    extra_args: list[str]
    additions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def record(self) -> dict:
        return {
            "kind": self.kind,
            "argv": list(self.argv),
            "extra_args": list(self.extra_args),
            "additions": list(self.additions),
            "warnings": list(self.warnings),
        }


def _tool_names(tools: str) -> list[str]:
    return [t for t in tools.replace(",", " ").split() if t]


def build_argv(
    kind: str,
    prompt: str,
    *,
    model: str | None = None,
    tools: str | None = None,
    permission_mode: str | None = None,
    permission_prompts: str | None = None,
    harness_prompt: str | None = None,
    extra_args: Iterable[str] = (),
) -> ClaudeCall:
    if kind not in KINDS:
        raise ValueError(f"unknown run kind {kind!r}; expected one of {KINDS}")
    extra = list(extra_args)
    passthrough, appended, warnings = _scan_passthrough(extra)

    additions: list[str] = []
    if kind == REWRITE:
        tools_val = "" if tools is None else tools
        mode = permission_mode or "dontAsk"
        prompts = permission_prompts
    elif kind == TRIGGER:
        tools_val = "Skill" if tools is None else tools
        mode = permission_mode or "dontAsk"
        prompts = permission_prompts
    else:
        tools_val = EXECUTION_DEFAULT_TOOLS if tools is None else tools
        mode = permission_mode or "acceptEdits"
        prompts = permission_prompts or "none"

    if kind != REWRITE and "Skill" not in _tool_names(tools_val):
        tools_val = f"{tools_val},Skill" if tools_val.strip() else "Skill"
        additions.append("--tools: appended Skill")

    if mode == "bypassPermissions":
        warnings.append("--permission-mode bypassPermissions: permission checks are bypassed")

    argv = ["claude", "-p", prompt, *_RESERVED_FLAGS]
    if model:
        argv += ["--model", model]
    argv += ["--tools", tools_val, "--permission-mode", mode]
    if prompts:
        argv += ["--permission-prompts", prompts]
    texts = ([harness_prompt] if harness_prompt else []) + appended
    if texts:
        argv += [_APPEND_PROMPT, "\n\n".join(texts)]
    argv += passthrough
    return ClaudeCall(kind, argv, extra, additions, warnings)


# --------------------------------------------------------------------------
# Temp projects and cleanup
# --------------------------------------------------------------------------

_state_lock = threading.RLock()
_projects: set[str] = set()
_children: set[subprocess.Popen] = set()
_prev_handlers: dict[int, object] = {}
_atexit_registered = False
_shutting_down = False
# While a thread is between creating a resource (child process or project dir)
# and registering it for cleanup, a signal handled on that same thread is
# deferred until the resource is tracked.
_spawning_thread: int | None = None
_deferred_signals: list[int] = []


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill a child this module started (it leads its own session)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


def _terminate_children() -> None:
    with _state_lock:
        children = list(_children)
    for proc in children:
        if proc.poll() is None:
            _kill_group(proc)


def _remove_registered_projects() -> None:
    with _state_lock:
        paths = list(_projects)
        _projects.clear()
    for p in paths:
        shutil.rmtree(p, ignore_errors=True)


def _cleanup_all() -> None:
    _terminate_children()
    _remove_registered_projects()


def _on_signal(signum: int, frame) -> None:
    global _shutting_down
    if _spawning_thread == threading.get_ident():
        _deferred_signals.append(signum)
        return
    _shutting_down = True
    _cleanup_all()
    prev = _prev_handlers.get(signum, signal.SIG_DFL)
    if callable(prev):
        # Python's default SIGINT handler raises KeyboardInterrupt, which
        # unwinds the caller's context managers.
        signal.signal(signum, prev)
        prev(signum, frame)
    elif prev == signal.SIG_IGN:
        return
    else:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)


def install_signal_handlers() -> None:
    """Idempotent. Main thread only; elsewhere a no-op."""
    global _atexit_registered
    with _state_lock:
        if not _atexit_registered:
            atexit.register(_cleanup_all)
            _atexit_registered = True
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            current = signal.getsignal(sig)
            if current is _on_signal:
                continue
            _prev_handlers[sig] = current
            signal.signal(sig, _on_signal)


def make_project() -> Path:
    global _spawning_thread
    install_signal_handlers()
    with _state_lock:
        if _shutting_down:
            raise ShuttingDown("signal received; not creating new projects")
        _spawning_thread = threading.get_ident()
        path = None
        try:
            path = Path(tempfile.mkdtemp(prefix=PROJECT_PREFIX))
            _projects.add(str(path))
            (path / PID_MARKER).write_text(str(os.getpid()))
            (path / ".claude" / "skills").mkdir(parents=True)
        except BaseException:
            if path is not None:
                _projects.discard(str(path))
                shutil.rmtree(path, ignore_errors=True)
            raise
        finally:
            _spawning_thread = None
    if _deferred_signals:
        _on_signal(_deferred_signals.pop(0), None)
    return path


def remove_project(path: Path | str) -> None:
    with _state_lock:
        _projects.discard(str(path))
    shutil.rmtree(path, ignore_errors=True)


def stage_skill(
    project: Path | str,
    name: str,
    *,
    source_dir: Path | str | None = None,
    skill_md: str | None = None,
) -> Path:
    """Stage a skill at <project>/.claude/skills/<name>/.

    source_dir is copied whole; skill_md (full frontmatter + body) is then
    written over SKILL.md. At least one is required.
    """
    if source_dir is None and skill_md is None:
        raise ValueError("stage_skill needs source_dir, skill_md, or both")
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"invalid skill name for staging: {name!r}")
    dest = Path(project) / ".claude" / "skills" / name
    if source_dir is not None:
        shutil.copytree(source_dir, dest, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    else:
        dest.mkdir(parents=True)
    if skill_md is not None:
        (dest / "SKILL.md").write_text(skill_md, encoding="utf-8")
    return dest


@contextlib.contextmanager
def temp_project(
    name: str | None = None,
    *,
    source_dir: Path | str | None = None,
    skill_md: str | None = None,
) -> Iterator[Path]:
    """Create a temp project (staging a skill when given); always remove it."""
    path = make_project()
    try:
        if source_dir is not None or skill_md is not None:
            if name is None:
                raise ValueError("temp_project needs name when staging a skill")
            stage_skill(path, name, source_dir=source_dir, skill_md=skill_md)
        yield path
    finally:
        remove_project(path)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sweep_stale(grace_s: float = 60.0) -> list[str]:
    """Remove skillz-run-* dirs whose marker pid is dead (covers SIGKILL).

    A dir with no readable marker is removed only after grace_s, so a project
    another process is still creating is not swept.
    """
    removed: list[str] = []
    root = Path(tempfile.gettempdir())
    for d in root.glob(f"{PROJECT_PREFIX}*"):
        if not d.is_dir() or d.is_symlink():
            continue
        try:
            pid = int((d / PID_MARKER).read_text().strip())
        except (OSError, ValueError):
            try:
                young = time.time() - d.stat().st_mtime < grace_s
            except OSError:
                continue
            if young:
                continue
            pid = 0
        if pid and _pid_alive(pid):
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed.append(str(d))
    return removed


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

@dataclass
class RawRun:
    returncode: int
    timed_out: bool
    stdout: str
    stderr_tail: str
    duration_s: float


def run_claude(
    call: ClaudeCall,
    cwd: Path | str,
    *,
    timeout: float,
    stream_path: Path | str | None = None,
    budget: "Budget | None" = None,
) -> RawRun:
    global _spawning_thread
    exe = shutil.which("claude")
    if exe is None:
        raise ClaudeNotFound("`claude` was not found on PATH")
    argv = [exe, *call.argv[1:]]
    start = time.monotonic()
    # Budget check, spawn and child registration are one critical section, so
    # a cost recorded by another thread cannot slip between check and launch,
    # and a signal cannot find the child untracked.
    with (budget.launch() if budget is not None else contextlib.nullcontext()):
        with _state_lock:
            if _shutting_down:
                raise ShuttingDown("signal received; not launching new claude calls")
            _spawning_thread = threading.get_ident()
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(cwd),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,  # lets us kill the whole group, and only our own
                )
                _children.add(proc)
            finally:
                _spawning_thread = None
    if _deferred_signals:
        _on_signal(_deferred_signals.pop(0), None)
    timed_out = False
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            out, err = proc.communicate()
    except BaseException:
        _kill_group(proc)
        proc.wait()
        raise
    finally:
        with _state_lock:
            _children.discard(proc)
    stdout = out.decode("utf-8", errors="replace")
    stderr = err.decode("utf-8", errors="replace")
    if stream_path is not None:
        Path(stream_path).write_text(stdout, encoding="utf-8")
    return RawRun(proc.returncode, timed_out, stdout, stderr[-STDERR_TAIL_CHARS:],
                  time.monotonic() - start)


def cli_version() -> str | None:
    exe = shutil.which("claude")
    if exe is None:
        return None
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


# --------------------------------------------------------------------------
# Stream parser
# --------------------------------------------------------------------------

@dataclass
class StreamSummary:
    init: dict | None = None
    result: dict | None = None
    tool_uses: list[dict] = field(default_factory=list)  # {"name", "input", "source"}
    denials: list[dict] = field(default_factory=list)
    bad_lines: int = 0

    @property
    def skills(self) -> list[str]:
        return list((self.init or {}).get("skills") or [])

    @property
    def model(self) -> str | None:
        return (self.init or {}).get("model")

    @property
    def tools(self) -> list[str]:
        return list((self.init or {}).get("tools") or [])

    @property
    def slash_commands(self) -> list[str]:
        return list((self.init or {}).get("slash_commands") or [])

    @property
    def claude_code_version(self) -> str | None:
        return (self.init or {}).get("claude_code_version")

    def _res(self, key):
        return None if self.result is None else self.result.get(key)

    @property
    def is_error(self) -> bool | None:
        v = self._res("is_error")
        return None if self.result is None else bool(v)

    @property
    def subtype(self) -> str | None:
        return self._res("subtype")

    @property
    def terminal_reason(self) -> str | None:
        return self._res("terminal_reason")

    @property
    def total_cost_usd(self) -> float | None:
        return self._res("total_cost_usd")

    @property
    def duration_ms(self) -> int | None:
        return self._res("duration_ms")

    @property
    def num_turns(self) -> int | None:
        return self._res("num_turns")

    @property
    def usage(self) -> dict | None:
        return self._res("usage")

    @property
    def total_tokens(self) -> int | None:
        if self.result is None:
            return None
        u = self.result.get("usage") or {}
        return sum(int(u.get(k) or 0) for k in (
            "input_tokens", "output_tokens",
            "cache_creation_input_tokens", "cache_read_input_tokens"))


def parse_stream(source: str | Iterable[str]) -> StreamSummary:
    lines = source.splitlines() if isinstance(source, str) else source
    s = StreamSummary()
    seen_denials: set[str] = set()
    partial: dict[int, dict] = {}  # content block index -> {"name", "buf", "input"}

    def add_denial(d: dict) -> None:
        key = d.get("tool_use_id")
        if key:
            if key in seen_denials:
                return
            seen_denials.add(key)
        s.denials.append(d)

    def finish_partial(index: int) -> None:
        blk = partial.pop(index, None)
        if blk is None:
            return
        inp = blk["input"]
        if blk["buf"].strip():
            try:
                parsed = json.loads(blk["buf"])
            except json.JSONDecodeError:
                return
            inp = parsed if isinstance(parsed, dict) else inp
        s.tool_uses.append({"name": blk["name"], "input": inp, "source": "partial"})

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            s.bad_lines += 1
            continue
        if not isinstance(ev, dict):
            s.bad_lines += 1
            continue
        etype = ev.get("type")
        if etype == "system":
            sub = ev.get("subtype")
            if sub == "init" and s.init is None:
                s.init = ev
            elif sub == "permission_denied":
                add_denial({"tool_name": ev.get("tool_name"),
                            "tool_use_id": ev.get("tool_use_id"),
                            "message": ev.get("message"), "source": "event"})
        elif etype == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    s.tool_uses.append({"name": block.get("name"),
                                        "input": block.get("input") or {},
                                        "source": "assistant"})
        elif etype == "stream_event":
            se = ev.get("event") or {}
            st = se.get("type")
            if st == "message_start":
                for idx in list(partial):
                    finish_partial(idx)
            elif st == "content_block_start":
                cb = se.get("content_block") or {}
                if cb.get("type") == "tool_use":
                    partial[se.get("index", 0)] = {
                        "name": cb.get("name"), "buf": "", "input": cb.get("input") or {}}
            elif st == "content_block_delta":
                delta = se.get("delta") or {}
                blk = partial.get(se.get("index", 0))
                if blk is not None and delta.get("type") == "input_json_delta":
                    blk["buf"] += delta.get("partial_json") or ""
            elif st == "content_block_stop":
                finish_partial(se.get("index", 0))
        elif etype == "result":
            s.result = ev
            for d in ev.get("permission_denials") or []:
                if isinstance(d, dict):
                    add_denial({"tool_name": d.get("tool_name"),
                                "tool_use_id": d.get("tool_use_id"),
                                "message": d.get("message"), "source": "result"})
                else:
                    add_denial({"tool_name": None, "tool_use_id": None,
                                "message": str(d), "source": "result"})
    for idx in list(partial):
        finish_partial(idx)
    return s


def _skill_matches(value, name: str) -> bool:
    if not isinstance(value, str):
        return False
    if value == name:
        return True
    prefix, sep, rest = value.partition(":")
    return bool(sep) and bool(prefix) and rest == name


def detect_trigger(summary: StreamSummary, skill_name: str, skill_path: Path | str | None = None) -> bool:
    """True when the full stream shows the skill being invoked or its SKILL.md read."""
    target = None
    if skill_path is not None:
        target = os.path.realpath(Path(skill_path) / "SKILL.md")
    for tu in summary.tool_uses:
        inp = tu.get("input") or {}
        if not isinstance(inp, dict):
            continue
        if tu.get("name") == "Skill" and _skill_matches(inp.get("skill"), skill_name):
            return True
        if tu.get("name") == "Read":
            fp = inp.get("file_path")
            if not isinstance(fp, str):
                continue
            if target is not None and os.path.realpath(fp) == target:
                return True
    return False


# --------------------------------------------------------------------------
# Outcome classification
# --------------------------------------------------------------------------

def classify(
    kind: str,
    returncode: int,
    timed_out: bool,
    summary: StreamSummary,
    triggered: bool | None = None,
) -> tuple[str, str | None]:
    """Return (status, error_reason). Never looks at result.subtype."""
    if timed_out:
        return "error", "timeout"
    if returncode != 0:
        return "error", f"exit code {returncode}"
    if summary.result is None:
        return "error", "no result event"
    if summary.is_error:
        return "error", "result.is_error is true"
    if summary.denials:
        tools = sorted({str(d.get("tool_name")) for d in summary.denials})
        return "error", f"permission denied: {', '.join(tools)}"
    if kind == TRIGGER:
        return ("triggered" if triggered else "not_triggered"), None
    return "ok", None


@dataclass
class RunOutcome:
    status: str
    error: str | None
    raw: RawRun
    summary: StreamSummary
    call: ClaudeCall

    @property
    def cost_usd(self) -> float:
        return self.summary.total_cost_usd or 0.0


def run_and_parse(
    call: ClaudeCall,
    cwd: Path | str,
    *,
    timeout: float,
    skill_name: str | None = None,
    skill_path: Path | str | None = None,
    stream_path: Path | str | None = None,
    budget: "Budget | None" = None,
) -> RunOutcome:
    raw = run_claude(call, cwd, timeout=timeout, stream_path=stream_path, budget=budget)
    summary = parse_stream(raw.stdout)
    if budget is not None:
        budget.add(summary.total_cost_usd)
    triggered = None
    if call.kind == TRIGGER:
        if skill_name is None:
            raise ValueError("trigger runs need skill_name")
        if skill_path is None:
            skill_path = Path(cwd) / ".claude" / "skills" / skill_name
        triggered = detect_trigger(summary, skill_name, skill_path)
    status, reason = classify(call.kind, raw.returncode, raw.timed_out, summary, triggered)
    return RunOutcome(status, reason, raw, summary, call)


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------

class Budget:
    """Cumulative total_cost_usd cap (--max-total-usd). Thread-safe."""

    def __init__(self, max_total_usd: float | None = None):
        if max_total_usd is not None and not (math.isfinite(max_total_usd) and max_total_usd >= 0):
            raise ValueError(f"max_total_usd must be a finite number >= 0, got {max_total_usd!r}")
        self.max_total_usd = max_total_usd
        self._spent = 0.0
        self._lock = threading.Lock()

    def add(self, cost_usd: float | None) -> None:
        """Add a run's total_cost_usd. A run with no valid cost (missing, NaN,
        negative) contributes nothing; its error is reported by classify()."""
        try:
            cost = float(cost_usd or 0.0)
        except (TypeError, ValueError):
            return
        if not math.isfinite(cost) or cost < 0:
            return
        with self._lock:
            self._spent += cost

    @property
    def spent(self) -> float:
        with self._lock:
            return self._spent

    @property
    def exceeded(self) -> bool:
        return self.max_total_usd is not None and self.spent >= self.max_total_usd

    def may_launch(self) -> bool:
        return not self.exceeded

    @contextlib.contextmanager
    def launch(self) -> Iterator[None]:
        """Hold the budget lock while a call is spawned; refuse once exceeded.

        add() takes the same lock, so no cost can be recorded between the
        check and the spawn.
        """
        with self._lock:
            if self.max_total_usd is not None and self._spent >= self.max_total_usd:
                raise BudgetExceeded(
                    f"spent ${self._spent:.4f} of the ${self.max_total_usd:.4f} cap")
            yield

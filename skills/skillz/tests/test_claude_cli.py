"""claude_cli: argv contract, passthrough, temp projects, runner, budget."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

import claude_cli as cc
from conftest import FIXTURES, SKILL_ROOT

STREAMS = FIXTURES / "stream-json"
EVAL_DIR = SKILL_ROOT / "scripts" / "eval"


def flag_value(argv, flag):
    return argv[argv.index(flag) + 1]


# ---------------------------------------------------------------- argv ----

@pytest.mark.parametrize("kind", cc.KINDS)
def test_c34_every_kind_carries_isolation_flags_and_tools_and_mode(kind):
    argv = cc.build_argv(kind, "q").argv
    assert argv[:2] == ["claude", "-p"]
    assert flag_value(argv, "--setting-sources") == "project"
    for f in ("--no-session-persistence", "--strict-mcp-config", "--verbose", "--include-partial-messages"):
        assert f in argv
    assert flag_value(argv, "--output-format") == "stream-json"
    assert "--tools" in argv and "--permission-mode" in argv


@pytest.mark.parametrize("kind", [cc.TRIGGER, cc.EXECUTION])
def test_c34_trigger_and_execution_tools_include_skill(kind):
    assert "Skill" in flag_value(cc.build_argv(kind, "q").argv, "--tools").split(",")


def test_c34_execution_passes_permission_prompts_none_and_acceptedits():
    argv = cc.build_argv(cc.EXECUTION, "q").argv
    assert flag_value(argv, "--permission-prompts") == "none"
    assert flag_value(argv, "--permission-mode") == "acceptEdits"


def test_trigger_defaults_match_verified_command():
    argv = cc.build_argv(cc.TRIGGER, "q", model="haiku").argv
    assert flag_value(argv, "--tools") == "Skill"
    assert flag_value(argv, "--permission-mode") == "dontAsk"
    assert flag_value(argv, "--model") == "haiku"
    assert "--permission-prompts" not in argv


def test_rewrite_is_tool_free_and_gets_no_skill():
    call = cc.build_argv(cc.REWRITE, "q")
    assert flag_value(call.argv, "--tools") == ""
    assert flag_value(call.argv, "--permission-mode") == "dontAsk"
    assert call.additions == []


def test_skill_appended_when_missing_and_recorded():
    call = cc.build_argv(cc.EXECUTION, "q", tools="Read,Write")
    assert flag_value(call.argv, "--tools") == "Read,Write,Skill"
    assert any("Skill" in a for a in call.additions)
    call = cc.build_argv(cc.TRIGGER, "q", tools="")
    assert flag_value(call.argv, "--tools") == "Skill"


def test_skill_not_appended_when_present_or_a_lookalike_is_present():
    call = cc.build_argv(cc.TRIGGER, "q", tools="Read,Skill")
    assert flag_value(call.argv, "--tools") == "Read,Skill" and call.additions == []
    call = cc.build_argv(cc.TRIGGER, "q", tools="Read,SkillFoo")
    assert flag_value(call.argv, "--tools") == "Read,SkillFoo,Skill"


def test_caller_first_class_values_win():
    call = cc.build_argv(cc.EXECUTION, "q", tools="Skill,Bash", permission_mode="plan", permission_prompts="deny")
    assert flag_value(call.argv, "--tools") == "Skill,Bash"
    assert flag_value(call.argv, "--permission-mode") == "plan"
    assert flag_value(call.argv, "--permission-prompts") == "deny"


def test_bypass_permissions_is_allowed_warned_and_recorded():
    call = cc.build_argv(cc.EXECUTION, "q", permission_mode="bypassPermissions")
    assert flag_value(call.argv, "--permission-mode") == "bypassPermissions"
    assert any("bypassPermissions" in w for w in call.warnings)
    call = cc.build_argv(cc.EXECUTION, "q", extra_args=["--dangerously-skip-permissions"])
    assert "--dangerously-skip-permissions" in call.argv
    assert any("--dangerously-skip-permissions" in w for w in call.warnings)
    assert call.record()["warnings"] == call.warnings


# ------------------------------------------------- passthrough, c46-c49 ----

def test_c46_passthrough_is_last_verbatim_and_in_order():
    extra = ["--allowedTools", "Bash(uv run *)", "--add-dir", "/x", "--zzz=1", "positional"]
    call = cc.build_argv(cc.EXECUTION, "q", model="m", harness_prompt="H", extra_args=extra)
    assert call.argv[-len(extra):] == extra
    assert call.extra_args == extra
    assert call.record()["extra_args"] == extra
    # every module flag precedes the passthrough
    assert call.argv.index("--permission-prompts") < len(call.argv) - len(extra)


RESERVED = ["-p", "--print", "--output-format", "--input-format", "--verbose",
            "--include-partial-messages", "--setting-sources", "--no-session-persistence",
            "--strict-mcp-config"]
INCOMPATIBLE = ["-c", "--continue", "-r", "--resume", "--fork-session", "--bg", "--background",
                "-w", "--worktree", "--tmux", "--cloud", "--teleport", "--desktop",
                "--remote-control", "--from-pr", "--environment", "--bare", "--safe-mode",
                "--disable-slash-commands"]
FIRST_CLASS = ["--model", "--tools", "--permission-mode", "--permission-prompts"]


@pytest.mark.parametrize("flag", RESERVED + INCOMPATIBLE + FIRST_CLASS)
@pytest.mark.parametrize("form", ["bare", "value", "equals"])
def test_c47_owned_flags_are_rejected_naming_the_flag(flag, form):
    extra = {"bare": [flag], "value": [flag, "x"], "equals": [f"{flag}=x"] if flag.startswith("--") else [flag, "x"]}[form]
    with pytest.raises(cc.PassthroughError) as e:
        cc.build_argv(cc.TRIGGER, "q", extra_args=["--ok", "1", *extra])
    assert flag in str(e.value)


@pytest.mark.parametrize("token", ["-rabc123", "-wmytree", "-w=name", "-cfoo", "-pfoo", "-r=abc"])
def test_c47_compact_short_option_forms_are_rejected(token):
    with pytest.raises(cc.PassthroughError) as e:
        cc.validate_passthrough([token])
    assert token[:2] in str(e.value)


def test_c47_unrelated_short_options_pass_through():
    cc.validate_passthrough(["-d", "api", "-h"])


@pytest.mark.parametrize("extra", [
    ["--add-dir", "../ext"], ["--add-dir=ext"], ["--add-dir", "/abs", "rel"], ["--add-dir", "~/ext"],
])
def test_relative_add_dir_is_refused(extra):
    """Calls run in a temp project, so a relative --add-dir names an unchecked directory."""
    with pytest.raises(cc.PassthroughError, match="absolute path"):
        cc.validate_passthrough(extra)


def test_add_dir_values_parses_both_forms():
    assert cc.add_dir_values(["--add-dir", "/a", "/b", "--x", "/c", "--add-dir=/d"]) == ["/a", "/b", "/d"]


def test_c47_message_gives_reason_or_script_option():
    with pytest.raises(cc.PassthroughError, match="isolation"):
        cc.validate_passthrough(["--setting-sources", "user"])
    with pytest.raises(cc.PassthroughError, match="--tools option"):
        cc.validate_passthrough(["--tools", "Bash"])
    with pytest.raises(cc.PassthroughError, match="never returns|isolated session"):
        cc.validate_passthrough(["--resume", "abc"])
    with pytest.raises(cc.PassthroughError, match="auto-triggering"):
        cc.validate_passthrough(["--bare"])


def test_c47_rejection_happens_before_any_cli_call(fake_claude):
    with pytest.raises(cc.PassthroughError):
        cc.build_argv(cc.TRIGGER, "q", extra_args=["--model", "x"])
    assert fake_claude.calls() == []


def test_c48_unknown_flag_passes_through_unchanged():
    extra = ["--some-future-flag", "value", "--other=1"]
    call = cc.build_argv(cc.TRIGGER, "q", extra_args=extra)
    assert call.argv[-3:] == extra
    cc.validate_passthrough(extra)


def test_c49_append_system_prompt_is_merged_after_harness_text():
    call = cc.build_argv(cc.EXECUTION, "q", harness_prompt="HARNESS",
                         extra_args=["--append-system-prompt", "one", "--keep", "--append-system-prompt=two"])
    merged = flag_value(call.argv, "--append-system-prompt")
    assert merged.startswith("HARNESS") and merged.index("one") < merged.index("two")
    assert call.argv.count("--append-system-prompt") == 1
    assert call.argv[-1] == "--keep"
    assert "two" not in call.argv  # not left in passthrough


def test_c49_caller_append_alone_and_no_append_at_all():
    call = cc.build_argv(cc.TRIGGER, "q", extra_args=["--append-system-prompt=only"])
    assert flag_value(call.argv, "--append-system-prompt") == "only"
    assert "--append-system-prompt" not in cc.build_argv(cc.TRIGGER, "q").argv
    with pytest.raises(cc.PassthroughError):
        cc.validate_passthrough(["--append-system-prompt"])


def test_c49_prompt_is_byte_identical():
    prompt = "  weird 'quotes' \"x\" \n\tunicode é --not-a-flag  "
    assert cc.build_argv(cc.TRIGGER, prompt).argv[2] == prompt
    assert cc.build_argv(cc.EXECUTION, prompt, harness_prompt="H").argv[2] == prompt


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        cc.build_argv("bogus", "q")


# --------------------------------------------------------- budget, c50 ----

def test_budget_unlimited_and_threshold():
    assert cc.Budget().may_launch()
    b = cc.Budget(1.0)
    b.add(0.4)
    assert b.may_launch() and not b.exceeded
    b.add(0.6)
    assert not b.may_launch() and b.exceeded and b.spent == pytest.approx(1.0)
    b.add(None)
    assert b.spent == pytest.approx(1.0)


def test_budget_rejects_bad_cap_and_ignores_bad_costs():
    for bad in (float("nan"), float("inf"), -1.0):
        with pytest.raises(ValueError):
            cc.Budget(bad)
    b = cc.Budget(1.0)
    for bad in (float("nan"), float("inf"), -5.0, "x", None):
        b.add(bad)
    assert b.spent == 0.0 and b.may_launch()
    b.add(1.5)
    assert not b.may_launch()


def test_budget_add_is_thread_safe():
    b = cc.Budget(None)
    ts = [threading.Thread(target=lambda: [b.add(0.001) for _ in range(500)]) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert b.spent == pytest.approx(4.0)


def test_c50_budget_stops_launching_with_fake_claude(fake_claude, tmp_area):
    fake_claude.respond({"cost": 0.4})
    budget = cc.Budget(1.0)
    launched = 0
    with cc.temp_project() as proj:
        while budget.may_launch() and launched < 20:
            cc.run_and_parse(cc.build_argv(cc.EXECUTION, "q"), proj, timeout=30, budget=budget)
            launched += 1
        assert launched == 3  # 0.4, 0.8, 1.2 -> crossed after the third
        assert budget.spent == pytest.approx(1.2)
        # a caller that skips may_launch() is still refused at spawn time
        with pytest.raises(cc.BudgetExceeded):
            cc.run_and_parse(cc.build_argv(cc.EXECUTION, "q"), proj, timeout=30, budget=budget)
    assert len(fake_claude.calls()) == 3


def test_c50_concurrent_launches_never_start_after_cap_crossed(fake_claude, tmp_area):
    """Check and spawn share the budget lock: calls started <= calls that fit + in flight."""
    fake_claude.respond({"cost": 0.5, "sleep": 0.3})
    budget = cc.Budget(1.0)
    started, refused = [], []
    lock = threading.Lock()
    with cc.temp_project() as proj:
        def worker():
            try:
                cc.run_and_parse(cc.build_argv(cc.EXECUTION, "q"), proj, timeout=30, budget=budget)
                with lock:
                    started.append(1)
            except cc.BudgetExceeded:
                with lock:
                    refused.append(1)
        # the first 4 workers overlap (all admitted before any finishes), then 4 more arrive late
        ts = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        late = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in late]
        [t.join() for t in late]
    assert len(started) == 4 and len(refused) == 4
    assert len(fake_claude.calls()) == 4


def test_budget_launch_context_blocks_add_until_spawn_done():
    b = cc.Budget(1.0)
    order = []
    with b.launch():
        t = threading.Thread(target=lambda: (b.add(2.0), order.append("added")))
        t.start()
        time.sleep(0.1)
        order.append("spawned")
    t.join()
    assert order == ["spawned", "added"]
    with pytest.raises(cc.BudgetExceeded):
        with b.launch():
            pass


def test_trigger_read_of_same_named_add_dir_skill_does_not_score(fake_claude, tmp_area, tmp_path):
    ext = tmp_path / "ext" / ".claude" / "skills" / "foo"
    ext.mkdir(parents=True)
    (ext / "SKILL.md").write_text("external")
    fake_claude.respond({"read_path": str(ext / "SKILL.md")})
    call = cc.build_argv(cc.TRIGGER, "q", extra_args=["--add-dir", str(tmp_path / "ext")])
    with cc.temp_project("foo", skill_md="x") as proj:
        o = cc.run_and_parse(call, proj, timeout=30, skill_name="foo")
        assert o.status == "not_triggered"
        # reading the staged candidate does score (path resolved through any symlinked tmp root)
        fake_claude.respond({"read_path": str(proj / ".claude" / "skills" / "foo" / "SKILL.md")})
        o = cc.run_and_parse(cc.build_argv(cc.TRIGGER, "q"), proj, timeout=30, skill_name="foo")
        assert o.status == "triggered"


# ---------------------------------------------------------- temp projects ----

def test_project_dir_layout_and_marker(tmp_area):
    p = cc.make_project()
    try:
        assert p.parent == tmp_area and p.name.startswith("skillz-run-")
        assert (p / ".skillz-pid").read_text() == str(os.getpid())
    finally:
        cc.remove_project(p)
    assert not p.exists()


def test_stage_skill_dir_and_md_text(tmp_area, tmp_path):
    src = tmp_path / "src"
    (src / "scripts").mkdir(parents=True)
    (src / "SKILL.md").write_text("old")
    (src / "scripts" / "a.py").write_text("x")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "junk.pyc").write_text("x")
    text = "---\nname: foo\ndescription: d\nwhen_to_use: w\n---\nbody\n"
    with cc.temp_project("foo", source_dir=src, skill_md=text) as p:
        staged = p / ".claude" / "skills" / "foo"
        assert (staged / "SKILL.md").read_text() == text
        assert (staged / "scripts" / "a.py").read_text() == "x"
        assert not (staged / "__pycache__").exists()
    assert (src / "SKILL.md").read_text() == "old"  # source untouched
    with cc.temp_project("bar", skill_md=text) as p:
        assert (p / ".claude" / "skills" / "bar" / "SKILL.md").read_text() == text


def test_stage_skill_rejects_bad_input(tmp_area):
    with cc.temp_project() as p:
        with pytest.raises(ValueError):
            cc.stage_skill(p, "x")
        for bad in ("../evil", "a/b", "", ".."):
            with pytest.raises(ValueError):
                cc.stage_skill(p, bad, skill_md="x")


def test_cleanup_on_normal_exit_and_on_exception(tmp_area):
    with cc.temp_project() as p:
        assert p.exists()
    assert not p.exists()
    with pytest.raises(RuntimeError):
        with cc.temp_project("foo", skill_md="x") as p2:
            raise RuntimeError("boom")
    assert not p2.exists()
    assert list(tmp_area.glob("skillz-run-*")) == []


@pytest.mark.parametrize("failing", ["marker", "skills_dir"])
def test_make_project_removes_dir_when_initialization_fails(tmp_area, monkeypatch, failing):
    real_write, real_mkdir = Path.write_text, Path.mkdir
    def bad_write(self, *a, **kw):
        if self.name == ".skillz-pid":
            raise OSError("disk full")
        return real_write(self, *a, **kw)
    def bad_mkdir(self, *a, **kw):
        if self.name == "skills":
            raise OSError("disk full")
        return real_mkdir(self, *a, **kw)
    monkeypatch.setattr(Path, "write_text", bad_write if failing == "marker" else real_write)
    monkeypatch.setattr(Path, "mkdir", bad_mkdir if failing == "skills_dir" else real_mkdir)
    with pytest.raises(OSError):
        cc.make_project()
    assert list(tmp_area.glob("skillz-run-*")) == []
    assert not cc._projects


def test_cleanup_when_staging_fails(tmp_area):
    with pytest.raises(ValueError):
        with cc.temp_project("../bad", skill_md="x"):
            pass
    assert list(tmp_area.glob("skillz-run-*")) == []


def test_sweep_removes_only_dead_pid_projects(tmp_area):
    live = cc.make_project()
    dead = tmp_area / "skillz-run-dead"
    dead.mkdir()
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    (dead / ".skillz-pid").write_text(str(done.pid))
    young_no_marker = tmp_area / "skillz-run-young"
    young_no_marker.mkdir()
    old_no_marker = tmp_area / "skillz-run-old"
    old_no_marker.mkdir()
    os.utime(old_no_marker, (1, 1))
    other = tmp_area / "unrelated-dir"
    other.mkdir()
    try:
        removed = cc.sweep_stale()
        assert set(map(os.path.basename, removed)) == {"skillz-run-dead", "skillz-run-old"}
        assert live.exists() and young_no_marker.exists() and other.exists()
        assert not dead.exists() and not old_no_marker.exists()
    finally:
        cc.remove_project(live)


# ------------------------------------------------- signals / SIGKILL ----

CHILD = textwrap.dedent("""
    import sys, threading, time
    sys.path.insert(0, {eval_dir!r})
    import claude_cli as cc
    proj = cc.make_project()
    print("PROJECT", proj, flush=True)
    def work():
        cc.run_claude(cc.build_argv(cc.EXECUTION, "q"), proj, timeout=120)
    t = threading.Thread(target=work, daemon=True)
    t.start()
    # wait until the shim has logged its call, then announce
    import os
    for _ in range(200):
        if os.path.exists(os.environ["FAKE_CLAUDE_LOG"]):
            break
        time.sleep(0.05)
    print("READY", flush=True)
    t.join()
""")


def start_child(fake_claude, tmp_area):
    fake_claude.respond({"sleep": 60})
    code = CHILD.format(eval_dir=str(EVAL_DIR))
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                         env={**os.environ, "TMPDIR": str(tmp_area)})
    proj = Path(p.stdout.readline().split(" ", 1)[1].strip())
    assert p.stdout.readline().strip() == "READY"
    assert proj.exists()
    return p, proj


def wait_dead(pid, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_c52_signal_removes_project_and_kills_own_claude_child(fake_claude, tmp_area, sig):
    p, proj = start_child(fake_claude, tmp_area)
    shim_pid = fake_claude.calls()[0]["pid"]
    p.send_signal(sig)
    assert p.wait(timeout=15) != 0
    assert not proj.exists()
    assert wait_dead(shim_pid), "claude child outlived the parent"
    assert list(tmp_area.glob("skillz-run-*")) == []


def test_c52_sigkill_leaves_dir_then_stale_sweep_removes_it(fake_claude, tmp_area):
    p, proj = start_child(fake_claude, tmp_area)
    shim_pid = fake_claude.calls()[0]["pid"]
    p.kill()
    p.wait()
    assert proj.exists()
    assert cc.sweep_stale() == [str(proj)]
    assert not proj.exists()
    os.killpg(shim_pid, signal.SIGKILL)  # test hygiene: the orphaned shim


# ----------------------------------------------------------------- runner ----

def test_run_claude_resolves_claude_on_path_at_call_time(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert cc.cli_version() is None
    with pytest.raises(cc.ClaudeNotFound):
        cc.run_claude(cc.build_argv(cc.TRIGGER, "q"), tmp_path, timeout=5)


def test_cli_version(fake_claude):
    assert cc.cli_version() == "2.1.300 (Claude Code)"
    fake_claude.set_rules([], version="9.9.9 (Claude Code)")
    assert cc.cli_version() == "9.9.9 (Claude Code)"


def test_run_records_argv_cwd_and_writes_stream_file(fake_claude, tmp_area, tmp_path):
    fake_claude.respond({"skill_call": "foo", "writes": {"outputs/x.txt": "hi"}})
    stream = tmp_path / "stream.jsonl"
    call = cc.build_argv(cc.EXECUTION, "do it", extra_args=["--zzz", "1"])
    with cc.temp_project("foo", skill_md="---\nname: foo\ndescription: d\n---\n") as proj:
        out = cc.run_and_parse(call, proj, timeout=30, stream_path=stream)
        assert (proj / "outputs" / "x.txt").read_text() == "hi"
        logged = fake_claude.calls()[0]
        assert logged["argv"] == call.argv[1:]
        assert os.path.realpath(logged["cwd"]) == os.path.realpath(proj)
    assert out.status == "ok" and out.error is None
    assert stream.read_text() == out.raw.stdout
    assert out.summary.skills.count("foo") == 1


def test_trigger_outcomes_via_shim(fake_claude, tmp_area):
    fake_claude.set_rules([
        {"match": "yes", "responses": [{"skill_call": "foo"}]},
        {"match": "glob", "responses": [{"tools_before": ["Glob"], "skill_call": "foo", "skill_partial": True}]},
        {"match": "no", "responses": [{}]},
    ])
    with cc.temp_project() as proj:
        def run(q):
            return cc.run_and_parse(cc.build_argv(cc.TRIGGER, q), proj, timeout=30, skill_name="foo")
        assert run("yes please").status == "triggered"
        assert run("glob first").status == "triggered"
        assert run("no thanks").status == "not_triggered"


def test_c26_infrastructure_failures_are_errors_not_non_triggers(fake_claude, tmp_area):
    fake_claude.set_rules([
        {"match": "exit", "responses": [{"exit_code": 1, "stderr": "boom: bad model id"}]},
        {"match": "iserr", "responses": [{"is_error": True, "subtype": "success"}]},
        {"match": "deny", "responses": [{"denial": "Write"}]},
        {"match": "nores", "responses": [{"no_result": True}]},
        {"match": "slow", "responses": [{"sleep": 30}]},
    ])
    with cc.temp_project() as proj:
        def run(q, timeout=30):
            return cc.run_and_parse(cc.build_argv(cc.TRIGGER, q), proj, timeout=timeout, skill_name="foo")
        o = run("exit")
        assert o.status == "error" and "exit code 1" in o.error and "bad model id" in o.raw.stderr_tail
        assert run("iserr").status == "error"
        assert run("deny").status == "error"
        assert run("nores").error == "no result event"
        t0 = time.monotonic()
        o = run("slow", timeout=1)
        assert o.status == "error" and o.error == "timeout" and o.raw.timed_out
        assert time.monotonic() - t0 < 10


def test_triggered_stream_but_nonzero_exit_is_error(fake_claude, tmp_area):
    fake_claude.respond({"skill_call": "foo", "exit_code": 1})
    with cc.temp_project() as proj:
        o = cc.run_and_parse(cc.build_argv(cc.TRIGGER, "q"), proj, timeout=30, skill_name="foo")
    assert o.status == "error"


def test_replay_fixture_through_runner(fake_claude, tmp_area):
    fake_claude.respond({"replay": str(STREAMS / "triggered_partial_default-sources.jsonl")})
    with cc.temp_project() as proj:
        o = cc.run_and_parse(cc.build_argv(cc.TRIGGER, "q"), proj, timeout=30, skill_name="zq-pelican-canary")
    assert o.status == "triggered"
    fake_claude.respond({"replay": str(STREAMS / "error_not-logged-in.jsonl"), "exit_code": 1,
                         "stderr": "Not logged in"})
    with cc.temp_project() as proj:
        o = cc.run_and_parse(cc.build_argv(cc.TRIGGER, "q"), proj, timeout=30, skill_name="zq-pelican-canary")
    assert o.status == "error" and "Not logged in" in o.raw.stderr_tail


def test_stderr_tail_is_bounded(fake_claude, tmp_area):
    fake_claude.respond({"stderr": "x" * 50000 + "END"})
    with cc.temp_project() as proj:
        o = cc.run_and_parse(cc.build_argv(cc.EXECUTION, "q"), proj, timeout=30)
    assert len(o.raw.stderr_tail) == cc.STDERR_TAIL_CHARS and o.raw.stderr_tail.endswith("END")


def test_shim_sequence_state_and_add_dir_skills(fake_claude, tmp_area, tmp_path):
    extra_root = tmp_path / "extra"
    (extra_root / ".claude" / "skills" / "leaky").mkdir(parents=True)
    fake_claude.respond({"cost": 0.1}, {"cost": 0.2})
    with cc.temp_project("mine", skill_md="x") as proj:
        call = cc.build_argv(cc.EXECUTION, "q", extra_args=["--add-dir", str(extra_root)])
        a = cc.run_and_parse(call, proj, timeout=30)
        b = cc.run_and_parse(call, proj, timeout=30)
        c = cc.run_and_parse(call, proj, timeout=30)
    assert [a.cost_usd, b.cost_usd, c.cost_usd] == [0.1, 0.2, 0.2]
    assert {"mine", "leaky"} <= set(a.summary.skills)


# --------------------------------------------------------- concurrency ----

def test_c31_concurrent_projects_never_see_each_others_skills(fake_claude, tmp_area):
    fake_claude.respond({"sleep": 0.2})
    n = 10
    results = {}
    errors = []

    def trial(i):
        try:
            name = f"cand-{i}"
            with cc.temp_project(name, skill_md=f"---\nname: {name}\ndescription: d{i}\n---\n") as proj:
                out = cc.run_and_parse(cc.build_argv(cc.TRIGGER, "q"), proj, timeout=60, skill_name=name)
                results[i] = (set(out.summary.skills), sorted(p.name for p in (proj / ".claude" / "skills").iterdir()))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=trial, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and len(results) == n
    for i, (init_skills, staged) in results.items():
        assert staged == [f"cand-{i}"]
        others = {f"cand-{j}" for j in range(n) if j != i}
        assert f"cand-{i}" in init_skills and not (init_skills & others)
    assert len(fake_claude.calls()) == n
    assert list(tmp_area.glob("skillz-run-*")) == []


def test_concurrent_shim_sequence_counter_is_race_free(fake_claude, tmp_area):
    n = 12
    fake_claude.respond(*[{"cost": float(k)} for k in range(n)])
    costs = []
    lock = threading.Lock()
    with cc.temp_project() as proj:
        def go():
            o = cc.run_and_parse(cc.build_argv(cc.EXECUTION, "q"), proj, timeout=60)
            with lock:
                costs.append(o.cost_usd)
        ts = [threading.Thread(target=go) for _ in range(n)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert sorted(costs) == [float(k) for k in range(n)]


RACE_CHILD = textwrap.dedent("""
    import os, signal, subprocess, sys
    sys.path.insert(0, {eval_dir!r})
    import claude_cli as cc
    proj = cc.make_project()
    print("PROJECT", proj, flush=True)
    real_popen = subprocess.Popen
    def popen_then_signal(*a, **kw):
        p = real_popen(*a, **kw)
        print("CHILD", p.pid, flush=True)
        os.kill(os.getpid(), signal.SIGTERM)  # lands after fork, before registration
        return p
    subprocess.Popen = popen_then_signal
    cc.run_claude(cc.build_argv(cc.EXECUTION, "q"), proj, timeout=120)
""")


def test_c52_signal_between_spawn_and_registration_still_kills_child(fake_claude, tmp_area):
    fake_claude.respond({"sleep": 60})
    p = subprocess.Popen([sys.executable, "-c", RACE_CHILD.format(eval_dir=str(EVAL_DIR))],
                         stdout=subprocess.PIPE, text=True, env={**os.environ, "TMPDIR": str(tmp_area)})
    out = p.stdout.read()
    assert p.wait(timeout=15) == -signal.SIGTERM
    proj = Path(out.split("PROJECT ", 1)[1].split("\n", 1)[0])
    child = int(out.split("CHILD ", 1)[1].split()[0])
    assert wait_dead(child), "child spawned just before the signal was orphaned"
    assert not proj.exists()


PROJECT_RACE_CHILD = textwrap.dedent("""
    import os, signal, sys, tempfile
    sys.path.insert(0, {eval_dir!r})
    import claude_cli as cc
    real = tempfile.mkdtemp
    def mkdtemp_then_signal(*a, **kw):
        d = real(*a, **kw)
        print("DIR", d, flush=True)
        os.kill(os.getpid(), signal.SIGTERM)  # lands before the dir is registered
        return d
    tempfile.mkdtemp = mkdtemp_then_signal
    cc.make_project()
""")


def test_c52_signal_during_project_creation_still_removes_it(tmp_area):
    p = subprocess.Popen([sys.executable, "-c", PROJECT_RACE_CHILD.format(eval_dir=str(EVAL_DIR))],
                         stdout=subprocess.PIPE, text=True, env={**os.environ, "TMPDIR": str(tmp_area)})
    out = p.stdout.read()
    assert p.wait(timeout=15) == -signal.SIGTERM
    assert not Path(out.split("DIR ", 1)[1].split("\n", 1)[0]).exists()
    assert list(tmp_area.glob("skillz-run-*")) == []

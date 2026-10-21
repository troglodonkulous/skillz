"""Tests for scripts/eval/run_tasks.py. All claude calls go to the fake shim."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import claude_cli
import run_tasks
from conftest import SKILL_ROOT

SCRIPT = SKILL_ROOT / "scripts" / "eval" / "run_tasks.py"
NAME = "demo-skill"
PROMPT = "Summarize report.txt\n  with  odd spacing, \"quotes\", unicode é and trailing space "


def make_skill(root: Path, name=NAME) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Demo skill used by tests.\n---\n\nBody.\n")
    return d


def write_evals(skill: Path, prompts=(PROMPT,), files=()):
    evals = [{"id": i + 1, "name": f"case {i + 1}", "prompt": p, "expected_output": "x",
              "files": list(files), "expectations": ["it works"]} for i, p in enumerate(prompts)]
    path = skill / "evals.json"
    path.write_text(json.dumps({"skill_name": NAME, "evals": evals}))
    return path


@pytest.fixture
def env(tmp_path, tmp_area, fake_claude):
    skill = make_skill(tmp_path / "src")
    (skill / "data").mkdir()
    (skill / "data" / "report.txt").write_text("hello")
    return {"skill": skill, "ws": tmp_path / "ws", "evals": write_evals(skill), "tmp": tmp_area,
            "fake": fake_claude, "root": tmp_path}


def run(env, *extra, yes=True, evals=None, baseline=None, runs=None, opts=(), timeout=120):
    cmd = [sys.executable, str(SCRIPT), "--evals", str(evals or env["evals"]),
           "--skill-path", str(env["skill"]), "--workspace", str(env["ws"]), "--json"]
    if baseline:
        cmd += ["--baseline", str(baseline)]
    if runs:
        cmd += ["--runs", str(runs)]
    cmd += list(opts)
    if yes:
        cmd.append("--yes")
    if extra:
        cmd += ["--", *extra]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def leftover(env):
    return sorted(p.name for p in env["tmp"].glob("skillz-run-*"))


def run_dirs(env):
    return sorted(env["ws"].glob("eval-*/*/run-*"))


def test_c51_no_yes_prints_plan_and_makes_no_calls(env):
    """Criterion 51."""
    env["fake"].respond({})
    r = run(env, yes=False, runs=3)
    assert r.returncode == 0, r.stderr
    plan = json.loads(r.stdout)
    assert plan["calls"] == 6 and plan["ran"] is False
    assert env["fake"].calls() == []
    assert not env["ws"].exists()
    assert leftover(env) == []


def test_c51_no_yes_text_mode_states_budget(env):
    cmd = [sys.executable, str(SCRIPT), "--evals", str(env["evals"]), "--skill-path", str(env["skill"]),
           "--workspace", str(env["ws"]), "--runs", "2"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0
    assert "1 evals x 2 configurations x 2 runs = 4 claude -p calls" in r.stdout
    assert "no --max-total-usd cap" in r.stdout
    assert env["fake"].calls() == []


def test_c46_passthrough_verbatim_in_order_after_module_flags(env):
    """Criterion 46."""
    env["fake"].respond({})
    r = run(env, "--some-future-flag", "value", "--allowedTools", "Bash(uv run *)")
    assert r.returncode == 0, r.stderr
    calls = env["fake"].calls()
    assert len(calls) == 2
    for c in calls:
        argv = c["argv"]
        tail = argv[-4:]
        assert tail == ["--some-future-flag", "value", "--allowedTools", "Bash(uv run *)"]
        assert argv.index("--permission-mode") < len(argv) - 4
    rec = json.loads((run_dirs(env)[0] / "run.json").read_text())
    assert rec["extra_args"] == ["--some-future-flag", "value", "--allowedTools", "Bash(uv run *)"]


def test_c49_prompt_verbatim_and_append_prompt_merged_after_harness(env):
    """Criterion 49."""
    env["fake"].respond({})
    r = run(env, "--append-system-prompt", "CALLER TEXT")
    assert r.returncode == 0, r.stderr
    for c in env["fake"].calls():
        argv = c["argv"]
        assert argv[0] == "-p" and argv[1] == PROMPT  # shim logs argv[1:]
        assert argv.count("--append-system-prompt") == 1
        text = argv[argv.index("--append-system-prompt") + 1]
        assert text.startswith(run_tasks.HARNESS_PROMPT)
        assert text.endswith("CALLER TEXT")
        assert "outputs/user_notes.md" in text and "./outputs/" in text


def test_c21_slash_invocation_prompt_reaches_argv_verbatim(env):
    """Criterion 21, argv level."""
    p = f"/{NAME} some $ARGUMENTS value"
    evals = write_evals(env["skill"], prompts=(p,))
    env["fake"].respond({})
    assert run(env, evals=evals).returncode == 0
    assert {c["argv"][1] for c in env["fake"].calls()} == {p}


def test_c34_every_call_has_mandatory_flags(env):
    """Criterion 34 for run_tasks calls."""
    env["fake"].respond({})
    assert run(env, runs=2).returncode == 0
    calls = env["fake"].calls()
    assert len(calls) == 4
    for c in calls:
        a = c["argv"]
        def val(flag):
            return a[a.index(flag) + 1]
        assert val("--setting-sources") == "project"
        assert "--no-session-persistence" in a and "--strict-mcp-config" in a
        assert val("--tools") == "Skill,Read,Write,Edit,Glob,Grep"
        assert val("--permission-mode") == "acceptEdits"
        assert val("--permission-prompts") == "none"
        assert "--model" not in a


def test_c34_skill_appended_to_custom_tools_and_recorded(env):
    env["fake"].respond({})
    assert run(env, opts=["--tools", "Read,Write", "--model", "m-1"]).returncode == 0
    for c in env["fake"].calls():
        a = c["argv"]
        assert a[a.index("--tools") + 1] == "Read,Write,Skill"
        assert a[a.index("--model") + 1] == "m-1"
    rec = json.loads((run_dirs(env)[0] / "run.json").read_text())
    assert rec["added_flags"] == ["--tools: appended Skill"]


def test_c52_artifacts_written_and_nothing_left_behind(env):
    """Criterion 52 (normal exit) plus the canonical layout."""
    env["fake"].respond({"writes": {"outputs/result.txt": "done", "outputs/user_notes.md": "n"},
                         "text": "all good"})
    r = run(env, "--allowedTools", "Bash(touch *)", runs=2)
    assert r.returncode == 0, r.stderr
    assert leftover(env) == []
    ev_dir = env["ws"] / "eval-1-case-1"
    meta = json.loads((ev_dir / "eval_metadata.json").read_text())
    assert meta["eval_id"] == 1 and meta["eval_name"] == "case 1" and meta["prompt"] == PROMPT
    assert meta["baseline_kind"] == "no_skill" and meta["baseline_label"] == "no skill"
    assert meta["expectations"] == ["it works"]
    assert len(run_dirs(env)) == 4
    for cfg in ("with_skill", "without_skill"):
        for k in (1, 2):
            d = ev_dir / cfg / f"run-{k}"
            for f in ("outputs/result.txt", "outputs/user_notes.md", "stream.jsonl", "run.json",
                      "timing.json", "metrics.json", "transcript.md"):
                assert (d / f).is_file(), (d, f)
            run_json = json.loads((d / "run.json").read_text())
            assert run_json["configuration"] == cfg
            assert run_json["argv"][2] == PROMPT
            assert run_json["extra_args"] == ["--allowedTools", "Bash(touch *)"]
            assert run_json["exit_code"] == 0 and run_json["is_error"] is False
            assert run_json["permission_mode"] == "acceptEdits"
            assert run_json["init"]["model"] == "fake-model"
            assert run_json["cli_version"] == "2.1.300"
            assert run_json["contaminated"] is False
            timing = json.loads((d / "timing.json").read_text())
            assert timing["total_tokens"] == 100 and timing["provenance"] == "cli_result"
            assert timing["duration_ms"] == 10 and timing["total_duration_seconds"] == 0.0
            metrics = json.loads((d / "metrics.json").read_text())
            assert metrics["files_created"] == ["result.txt", "user_notes.md"]
            assert metrics["total_steps"] >= 1
            tr = (d / "transcript.md").read_text()
            assert tr.startswith("## Eval Prompt") and PROMPT in tr and "all good" in tr
            assert metrics["transcript_chars"] == len(tr)


def test_c52_missing_result_event_is_wall_clock_with_null_tokens(env):
    env["fake"].respond({"no_result": True})
    assert run(env).returncode == 0
    d = run_dirs(env)[0]
    timing = json.loads((d / "timing.json").read_text())
    assert timing["total_tokens"] is None and timing["provenance"] == "wall_clock"
    rec = json.loads((d / "run.json").read_text())
    assert rec["is_error"] is None and rec["status"] == "error"


def test_run_error_recorded_not_dropped(env):
    env["fake"].respond({"denial": "Bash"})
    assert run(env).returncode == 0
    rec = json.loads((run_dirs(env)[0] / "run.json").read_text())
    assert rec["status"] == "error" and "Bash" in rec["error"]
    assert rec["permission_denied_events"] == 1 and len(rec["permission_denials"]) == 1


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_c52_no_project_left_after_signal(env, sig):
    """Criterion 52, SIGINT and SIGTERM."""
    env["fake"].respond({"sleep": 30})
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--evals", str(env["evals"]), "--skill-path", str(env["skill"]),
         "--workspace", str(env["ws"]), "--yes"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 30
    while time.time() < deadline and len(env["fake"].calls()) < 2:
        time.sleep(0.05)
    assert len(env["fake"].calls()) == 2
    assert leftover(env), "projects should exist while runs are in flight"
    proc.send_signal(sig)
    proc.communicate(timeout=30)
    assert proc.returncode != 0
    assert leftover(env) == []


def test_c52_sigkill_leftover_swept_on_next_start(env):
    """Criterion 52, SIGKILL then next start."""
    env["fake"].respond({"sleep": 8})
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--evals", str(env["evals"]), "--skill-path", str(env["skill"]),
         "--workspace", str(env["ws"]), "--yes"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 30
    while time.time() < deadline and len(env["fake"].calls()) < 2:
        time.sleep(0.05)
    proc.kill()
    proc.wait()
    assert leftover(env), "SIGKILL cannot clean up"
    r = run(env, yes=False)  # a fresh start sweeps, even when it spends nothing
    assert r.returncode == 0
    assert leftover(env) == []


def test_c50_max_total_usd_stops_new_launches_in_flight_finish(env):
    """Criterion 50."""
    env["fake"].respond({"cost": 1.0, "sleep": 0.5})
    evals = write_evals(env["skill"], prompts=("a", "b"))
    r = run(env, evals=evals, runs=6, opts=["--max-total-usd", "1.5"])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    launched = len(env["fake"].calls())
    assert out["calls"] == 24
    assert 1 <= launched < 24
    assert out["recorded"] == launched  # every launched run, including in-flight ones, is recorded
    assert len(run_dirs(env)) == launched
    assert out["skipped_by_budget"] == 24 - launched
    assert out["spent_usd"] == pytest.approx(launched * 1.0)
    assert leftover(env) == []


def test_c50_zero_cap_launches_nothing(env):
    env["fake"].respond({})
    r = run(env, opts=["--max-total-usd", "0"])
    assert r.returncode == 0
    assert env["fake"].calls() == [] and json.loads(r.stdout)["skipped_by_budget"] == 2


def test_c20_baseline_that_sees_candidate_via_add_dir_is_flagged(env):
    """Criterion 20 recording: run.json marks the contaminated baseline."""
    leak = env["root"] / "leak"
    make_skill(leak / ".claude" / "skills")
    env["fake"].respond({})
    r = run(env, "--add-dir", str(leak))
    assert r.returncode == 0, r.stderr
    ev = env["ws"] / "eval-1-case-1"
    base = json.loads((ev / "without_skill" / "run-1" / "run.json").read_text())
    cand = json.loads((ev / "with_skill" / "run-1" / "run.json").read_text())
    assert NAME in base["init"]["skills"]
    assert base["contaminated"] is True and base["status"] == "contaminated"
    assert cand["contaminated"] is False


def test_c20_clean_baseline_not_flagged_and_candidate_visible_only_to_with_skill(env):
    env["fake"].respond({})
    assert run(env).returncode == 0
    ev = env["ws"] / "eval-1-case-1"
    base = json.loads((ev / "without_skill" / "run-1" / "run.json").read_text())
    cand = json.loads((ev / "with_skill" / "run-1" / "run.json").read_text())
    assert NAME not in base["init"]["skills"] and base["contaminated"] is False
    assert NAME in cand["init"]["skills"]


def test_snapshot_baseline_metadata_and_not_self_contaminated(env):
    snap = make_skill(env["root"] / "snapshots", NAME)
    snap_dir = env["root"] / "snapshots" / "v1-original"
    snap.rename(snap_dir)
    env["fake"].respond({})
    r = run(env, baseline=snap_dir)
    assert r.returncode == 0, r.stderr
    ev = env["ws"] / "eval-1-case-1"
    meta = json.loads((ev / "eval_metadata.json").read_text())
    assert meta["baseline_kind"] == "snapshot" and meta["baseline_label"] == "snapshot: v1-original"
    assert (ev / "without_skill" / "run-1").is_dir()
    base = json.loads((ev / "without_skill" / "run-1" / "run.json").read_text())
    assert NAME in base["init"]["skills"]  # the snapshot is staged under the same name
    assert base["contaminated"] is False


def test_snapshot_baseline_with_leaked_candidate_under_other_name_is_flagged(env):
    """A snapshot named differently from the candidate must not hide a leak."""
    snap_dir = make_skill(env["root"] / "snapshots", "old-name")
    leak = env["root"] / "leak"
    make_skill(leak / ".claude" / "skills")
    env["fake"].respond({})
    assert run(env, "--add-dir", str(leak), baseline=snap_dir).returncode == 0
    base = json.loads((env["ws"] / "eval-1-case-1" / "without_skill" / "run-1" / "run.json").read_text())
    assert base["contaminated"] is True


def test_staging_per_configuration_and_input_files_copied(env, monkeypatch):
    """Each run's project holds only its configuration's skill; eval files are copied in."""
    seen = []
    real = claude_cli.run_and_parse

    def spy(call, cwd, **kw):
        cwd = Path(cwd)
        seen.append({
            "skills": sorted(p.name for p in (cwd / ".claude" / "skills").iterdir()),
            "file": (cwd / "data" / "report.txt").read_text() if (cwd / "data" / "report.txt").exists() else None,
            "prompt": call.argv[2],
        })
        return real(call, cwd, **kw)

    monkeypatch.setattr(claude_cli, "run_and_parse", spy)
    env["fake"].respond({})
    evals = write_evals(env["skill"], files=["data/report.txt"])
    snap_dir = make_skill(env["root"] / "snapshots", "old-name")
    plan = run_tasks.Plan(skill_dir=env["skill"], skill_name=NAME, baseline_kind="snapshot",
                          baseline_dir=snap_dir, baseline_name="old-name", model=None, tools=None,
                          permission_mode=None, extra_args=[], skill_root=env["skill"])
    ev = json.loads(evals.read_text())["evals"][0]
    budget = claude_cli.Budget(None)
    run_tasks.execute_run(plan, ev, "with_skill", env["ws"] / "a", budget)
    run_tasks.execute_run(plan, ev, "without_skill", env["ws"] / "b", budget)
    plan.baseline_kind, plan.baseline_dir, plan.baseline_name = "no_skill", None, None
    run_tasks.execute_run(plan, ev, "without_skill", env["ws"] / "c", budget)
    assert [s["skills"] for s in seen] == [[NAME], ["old-name"], []]
    assert all(s["file"] == "hello" and s["prompt"] == PROMPT for s in seen)
    assert leftover(env) == []


def test_jobs_interleave_configurations():
    evals = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    dirs = {1: Path("e1"), 2: Path("e2")}
    jobs = run_tasks.build_jobs(evals, 2, dirs)
    assert [(j[0]["id"], j[1], j[2]) for j in jobs[:4]] == [
        (1, "with_skill", 1), (1, "without_skill", 1), (2, "with_skill", 1), (2, "without_skill", 1)]
    assert [j[2] for j in jobs] == [1] * 4 + [2] * 4


def test_runs_execute_concurrently(env):
    """Six one-second runs finish in well under six seconds."""
    env["fake"].respond({"sleep": 1.0})
    evals = write_evals(env["skill"], prompts=("a", "b", "c"))
    start = time.monotonic()
    assert run(env, evals=evals).returncode == 0
    assert time.monotonic() - start < 4.5
    assert len(run_dirs(env)) == 6


def test_passthrough_flag_rejected_before_any_call(env):
    env["fake"].respond({})
    for flags in (["--model", "x"], ["--output-format", "json"], ["--resume", "abc"]):
        r = run(env, *flags)
        assert r.returncode != 0
        assert flags[0] in r.stderr
    assert env["fake"].calls() == [] and not env["ws"].exists()


def test_existing_run_dir_refused_before_any_call(env):
    env["fake"].respond({})
    assert run(env).returncode == 0
    n = len(env["fake"].calls())
    r = run(env)
    assert r.returncode != 0 and "already exist" in r.stderr
    assert len(env["fake"].calls()) == n


def test_missing_input_file_fails_before_any_call(env):
    evals = write_evals(env["skill"], files=["nope.txt"])
    r = run(env, evals=evals)
    assert r.returncode != 0 and "nope.txt" in r.stderr
    assert env["fake"].calls() == []


def test_help_lists_options(env):
    r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    for opt in ("--evals", "--skill-path", "--workspace", "--baseline", "--runs", "--model", "--tools",
                "--permission-mode", "--max-total-usd", "--yes", "--json"):
        assert opt in r.stdout


def test_c20_same_name_snapshot_with_candidate_in_add_dir_is_flagged(env):
    """Criterion 20: a same-named snapshot must not mask a leaked candidate."""
    snap_dir = make_skill(env["root"] / "snapshots", NAME)
    leak = env["root"] / "leak"
    make_skill(leak / ".claude" / "skills")
    env["fake"].respond({})
    assert run(env, "--add-dir", str(leak), baseline=snap_dir / ".." / NAME).returncode == 0
    ev = env["ws"] / "eval-1-case-1"
    base = json.loads((ev / "without_skill" / "run-1" / "run.json").read_text())
    cand = json.loads((ev / "with_skill" / "run-1" / "run.json").read_text())
    assert base["contaminated"] is True and base["status"] == "contaminated"
    assert cand["contaminated"] is False


def test_timing_stream_and_run_json_survive_output_copy_failure(env, monkeypatch):
    """Artifacts are written before outputs are copied; a copy failure is a recorded run error."""
    real = claude_cli.run_and_parse

    def sabotage(call, cwd, **kw):
        out = real(call, cwd, **kw)
        import shutil as sh
        sh.rmtree(Path(cwd) / "outputs")  # the copy will now fail
        return out

    monkeypatch.setattr(claude_cli, "run_and_parse", sabotage)
    env["fake"].respond({})
    ev = json.loads(env["evals"].read_text())["evals"][0]
    plan = run_tasks.Plan(skill_dir=env["skill"], skill_name=NAME, baseline_kind="no_skill",
                          baseline_dir=None, baseline_name=None, model=None, tools=None,
                          permission_mode=None, extra_args=[], skill_root=env["skill"])
    rec = run_tasks.execute_run(plan, ev, "with_skill", env["ws"] / "r", claude_cli.Budget(None))
    d = env["ws"] / "r"
    assert json.loads((d / "timing.json").read_text())["total_tokens"] == 100
    assert (d / "stream.jsonl").read_text().strip()
    run_json = json.loads((d / "run.json").read_text())
    assert run_json["status"] == "error" and "output_copy_error" in run_json
    assert rec["status"] == "error"
    assert (d / "outputs").is_dir() and (d / "metrics.json").is_file() and (d / "transcript.md").is_file()
    assert leftover(env) == []


def test_baseline_same_directory_as_candidate_rejected_before_any_call(env):
    """A candidate-vs-itself baseline is refused."""
    env["fake"].respond({})
    r = run(env, baseline=env["skill"])
    assert r.returncode != 0 and "same directory" in r.stderr
    assert env["fake"].calls() == [] and not env["ws"].exists()
    link = env["root"] / "alias"
    link.symlink_to(env["skill"])
    assert run(env, baseline=link).returncode != 0


def test_unexpected_runner_failure_leaves_error_run_directory(env, monkeypatch):
    """A failure before or during the call is a recorded error run, not an absent one."""
    def boom(call, cwd, **kw):
        raise RuntimeError("spawn exploded")

    monkeypatch.setattr(claude_cli, "run_and_parse", boom)
    ev = json.loads(env["evals"].read_text())["evals"][0]
    d = env["ws"] / "r"
    run_tasks.record_failure("with_skill", ev, d, RuntimeError("spawn exploded"))
    rj = json.loads((d / "run.json").read_text())
    assert rj["status"] == "error" and "spawn exploded" in rj["error"] and rj["configuration"] == "with_skill"
    for f in ("timing.json", "stream.jsonl", "transcript.md", "metrics.json"):
        assert (d / f).is_file()
    assert (d / "outputs").is_dir()
    assert json.loads((d / "timing.json").read_text())["total_tokens"] is None


def test_runner_failure_end_to_end_records_error_run_and_exits_nonzero(env):
    """A baseline that cannot be staged still shows up as an error run directory."""
    snap = make_skill(env["root"] / "snapshots", "old-name")
    bad = snap / "unreadable.txt"
    bad.write_text("x")
    bad.chmod(0)
    try:
        env["fake"].respond({})
        r = run(env, baseline=snap)
    finally:
        bad.chmod(0o644)
    assert r.returncode == 1
    ev = env["ws"] / "eval-1-case-1"
    rj = json.loads((ev / "without_skill" / "run-1" / "run.json").read_text())
    assert rj["status"] == "error" and "runner failure" in rj["error"]
    assert (ev / "without_skill" / "run-1" / "outputs").is_dir()
    assert json.loads((ev / "with_skill" / "run-1" / "run.json").read_text())["status"] == "ok"
    assert leftover(env) == []


def test_baseline_without_init_inventory_fails_closed(env):
    """No init event: isolation cannot be verified, so the baseline is an error, not ok."""
    stream = env["root"] / "no_init.jsonl"
    stream.write_text(json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "duration_ms": 1, "num_turns": 1,
        "result": "ok", "total_cost_usd": 0.001, "permission_denials": [],
        "usage": {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0}}) + "\n")
    env["fake"].respond({"replay": str(stream)})
    assert run(env).returncode == 0
    ev = env["ws"] / "eval-1-case-1"
    base = json.loads((ev / "without_skill" / "run-1" / "run.json").read_text())
    assert base["init"]["skills"] is None
    assert base["status"] == "error" and "isolation" in base["error"]


def test_scheduler_starts_both_configurations_before_either_finishes(env, monkeypatch):
    """With two workers the first two launches are one of each configuration."""
    from click.testing import CliRunner
    order = []
    real = claude_cli.run_and_parse

    def spy(call, cwd, **kw):
        order.append("with_skill" if any((Path(cwd) / ".claude" / "skills").iterdir()) else "without_skill")
        return real(call, cwd, **kw)

    monkeypatch.setattr(claude_cli, "run_and_parse", spy)
    monkeypatch.setattr(run_tasks, "MAX_WORKERS", 2)
    env["fake"].respond({"sleep": 0.3})
    evals = write_evals(env["skill"], prompts=("a", "b", "c"))
    res = CliRunner().invoke(run_tasks.main, [
        "--evals", str(evals), "--skill-path", str(env["skill"]),
        "--workspace", str(env["ws"]), "--yes", "--json"])
    assert res.exit_code == 0, res.output
    assert len(order) == 6
    assert set(order[:2]) == {"with_skill", "without_skill"}


def test_c44_workspace_inside_any_skill_tree_rejected(env, tmp_path):
    """Criterion 44: another skill, a .claude/skills tree, a plugin's skills/ dir, or a symlink into one."""
    other = make_skill(tmp_path / "home" / ".claude" / "skills", "other-skill")
    plugin_skills = tmp_path / "plugin" / "skills"
    (tmp_path / "plugin" / ".claude-plugin").mkdir(parents=True)
    plugin_skills.mkdir()
    link = tmp_path / "link"
    link.symlink_to(other)
    env["fake"].respond({})
    for ws in (other / "ws", other.parent / "ws", plugin_skills / "ws", link / "ws"):
        env["ws"] = ws
        r = run(env)
        assert r.returncode != 0 and "--workspace must not be" in r.stderr, ws
        assert not ws.exists()
    assert env["fake"].calls() == []


def test_c44_sibling_workspace_outside_skills_dirs_allowed(env):
    env["fake"].respond({})
    env["ws"] = env["skill"].parent / f"{env['skill'].name}-workspace" / "iteration-1"
    r = run(env)
    assert r.returncode == 0, r.stderr


def test_workspace_inside_skill_or_snapshot_rejected_before_any_call(env):
    snap = make_skill(env["root"] / "snapshots", "old-name")
    env["fake"].respond({})
    for ws, baseline in ((env["skill"], None), (env["skill"] / "ws", None), (snap / "ws", snap)):
        env["ws"] = ws
        r = run(env, baseline=baseline)
        assert r.returncode != 0 and "--workspace must not be" in r.stderr
    assert env["fake"].calls() == [] and not (env["skill"] / "ws").exists() and not (snap / "ws").exists()
    assert not (env["skill"] / "eval-1-case-1").exists()

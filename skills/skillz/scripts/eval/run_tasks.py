#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["click", "pyyaml"]
# ///
"""Execution runner: one isolated `claude -p` session per eval x configuration x run.

Every run gets its own temp project holding only its configuration's skill
(candidate for with_skill, the snapshot for a snapshot baseline, nothing for
no_skill). All runs launch together through a bounded thread pool with the
configurations interleaved. After each run the runner writes outputs/,
stream.jsonl, run.json, timing.json, transcript.md and metrics.json (see
references/schemas.md). Without --yes it prints the plan and budget and makes
no claude call.

Passthrough after a literal `--` goes to `claude -p` verbatim (spec 4.5).
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import claude_cli  # noqa: E402
import validation  # noqa: E402

MAX_WORKERS = 8
RUN_TIMEOUT_S = 3600
TOOL_RESULT_CHARS = 2000

HARNESS_PROMPT = (
    "Save every deliverable file under ./outputs/ (create the directory if needed). "
    "Before you finish, write ./outputs/user_notes.md: your own account of "
    "uncertainties, workarounds, and anything a human should review."
)

WITH_SKILL = "with_skill"
WITHOUT_SKILL = "without_skill"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "eval"


def _fail(msg: str) -> "click.ClickException":
    return click.ClickException(msg)


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def _skill_name(skill_dir: Path, role: str) -> str:
    md = skill_dir / "SKILL.md"
    if not md.is_file():
        raise _fail(f"{role}: no SKILL.md in {skill_dir}")
    fm, _body, err = validation.parse_frontmatter(md.read_text(encoding="utf-8"))
    if fm is None or not isinstance(fm.get("name"), str) or not fm["name"].strip():
        raise _fail(f"{role}: SKILL.md in {skill_dir} has no usable `name` frontmatter ({err or 'missing name'})")
    return fm["name"].strip()


def _load_evals(path: Path, skill_root: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise _fail(f"cannot read {path}: {e}")
    evals = data.get("evals") if isinstance(data, dict) else None
    if not isinstance(evals, list) or not evals:
        raise _fail(f"{path}: `evals` must be a non-empty list")
    seen: set[int] = set()
    for i, ev in enumerate(evals):
        if not isinstance(ev, dict):
            raise _fail(f"{path}: evals[{i}] is not an object")
        if not isinstance(ev.get("id"), int) or isinstance(ev.get("id"), bool):
            raise _fail(f"{path}: evals[{i}].id must be an integer")
        if ev["id"] in seen:
            raise _fail(f"{path}: duplicate eval id {ev['id']}")
        seen.add(ev["id"])
        if not isinstance(ev.get("name"), str) or not ev["name"].strip():
            raise _fail(f"{path}: evals[{i}].name must be a non-empty string")
        if not isinstance(ev.get("prompt"), str) or not ev["prompt"]:
            raise _fail(f"{path}: evals[{i}].prompt must be a non-empty string")
        files = ev.get("files", [])
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise _fail(f"{path}: evals[{i}].files must be a list of strings")
        for rel in files:
            src = (skill_root / rel).resolve()
            if not src.is_relative_to(skill_root.resolve()):
                raise _fail(f"eval {ev['id']}: file {rel!r} is outside the skill root")
            if not src.exists():
                raise _fail(f"eval {ev['id']}: input file {rel!r} not found under {skill_root}")
    return evals


# --------------------------------------------------------------------------
# Rendering from the stream
# --------------------------------------------------------------------------

def _clip(text: str) -> str:
    if len(text) <= TOOL_RESULT_CHARS:
        return text
    return text[:TOOL_RESULT_CHARS] + f"\n... [{len(text) - TOOL_RESULT_CHARS} more characters truncated]"


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return "" if content is None else str(content)


def render_stream(stream_text: str, prompt: str) -> tuple[str, dict]:
    """Return (transcript.md text, partial metrics: tool_calls, steps, errors)."""
    lines = ["## Eval Prompt", "", prompt, ""]
    tool_calls: dict[str, int] = {}
    steps = 0
    errors = 0
    result_ev = None
    for raw in stream_text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        message = ev.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if etype == "assistant":
            steps += 1
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    lines += ["## Assistant", "", str(b.get("text", "")), ""]
                elif b.get("type") == "tool_use":
                    name = str(b.get("name"))
                    tool_calls[name] = tool_calls.get(name, 0) + 1
                    lines += [f"### Tool call: {name}", "", "```json",
                              json.dumps(b.get("input") or {}, indent=2, ensure_ascii=False), "```", ""]
        elif etype == "user":
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    is_err = bool(b.get("is_error"))
                    errors += is_err
                    lines += ["### Tool result" + (" (error)" if is_err else ""), "", "```",
                              _clip(_result_text(b.get("content"))), "```", ""]
        elif etype == "result":
            result_ev = ev
    if result_ev is not None:
        lines += ["## Result", "",
                  f"subtype: {result_ev.get('subtype')}, is_error: {result_ev.get('is_error')}, "
                  f"turns: {result_ev.get('num_turns')}, "
                  f"cost_usd: {result_ev.get('total_cost_usd')}", ""]
        if result_ev.get("result"):
            lines += [str(result_ev["result"]), ""]
    else:
        lines += ["## Result", "", "No result event was emitted.", ""]
    return "\n".join(lines), {"tool_calls": tool_calls, "steps": steps, "errors": errors}


def _output_stats(outputs: Path) -> tuple[list[str], int]:
    files: list[str] = []
    chars = 0
    for p in sorted(outputs.rglob("*")):
        if not p.is_file():
            continue
        files.append(p.relative_to(outputs).as_posix())
        try:
            chars += len(p.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, OSError):
            pass  # binary file: counted in files_created, not in output_chars
    return files, chars


# --------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------

def build_jobs(evals: list[dict], runs: int, eval_dirs: dict) -> list[tuple]:
    """(eval, configuration, run number, run dir), configurations interleaved:
    both configurations of an eval are adjacent, and run k of everything
    precedes run k+1."""
    return [(ev, config, k, eval_dirs[ev["id"]] / config / f"run-{k}")
            for k in range(1, runs + 1) for ev in evals for config in (WITH_SKILL, WITHOUT_SKILL)]


class Plan:
    def __init__(self, *, skill_dir, skill_name, baseline_kind, baseline_dir, baseline_name,
                 model, tools, permission_mode, extra_args, skill_root):
        self.skill_dir = skill_dir
        self.skill_name = skill_name
        self.baseline_kind = baseline_kind
        self.baseline_dir = baseline_dir
        self.baseline_name = baseline_name
        self.model = model
        self.tools = tools
        self.permission_mode = permission_mode
        self.extra_args = extra_args
        self.skill_root = skill_root


def _build_call(plan: Plan, prompt: str) -> claude_cli.ClaudeCall:
    return claude_cli.build_argv(
        claude_cli.EXECUTION, prompt,
        model=plan.model, tools=plan.tools, permission_mode=plan.permission_mode,
        harness_prompt=HARNESS_PROMPT, extra_args=plan.extra_args)


def _argv_value(argv: list[str], flag: str) -> str | None:
    for i, a in enumerate(argv[:-1]):
        if a == flag:
            return argv[i + 1]
    return None


def _is_contaminated(plan: Plan, config: str, skills: list[str] | None) -> bool:
    """A baseline is contaminated when it could see the candidate. A snapshot
    that shares the candidate's name makes the name alone ambiguous, so that
    case also looks for a second copy in init.skills and for the candidate in
    an --add-dir; it fails closed."""
    if config != WITHOUT_SKILL:
        return False
    same_name = plan.baseline_kind == "snapshot" and plan.baseline_name == plan.skill_name
    if same_name:
        exposed = any((Path(d) / ".claude" / "skills" / plan.skill_name).exists()
                      for d in claude_cli.add_dir_values(plan.extra_args))
        return exposed or (skills is not None and skills.count(plan.skill_name) > 1)
    return skills is not None and plan.skill_name in skills


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def record_failure(config: str, ev: dict, run_dir: Path, exc: BaseException) -> None:
    """An unexpected runner failure still leaves a run directory marked as an
    error, so it is counted and listed rather than silently absent. Files
    already written are kept."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "outputs").mkdir(exist_ok=True)
    reason = f"runner failure: {type(exc).__name__}: {exc}"
    rj = run_dir / "run.json"
    if rj.exists():
        data = json.loads(rj.read_text(encoding="utf-8"))
    else:
        data = {"configuration": config, "exit_code": None, "stderr_tail": "", "is_error": None,
                "terminal_reason": None, "permission_denials": [], "permission_denied_events": 0,
                "init": {"skills": None, "model": None, "tools": None, "permission_mode": None},
                "cli_version": None, "argv": [], "extra_args": [], "permission_mode": None,
                "added_flags": [], "total_cost_usd": None, "num_turns": None, "contaminated": False}
    data["status"] = "error"
    data["error"] = data.get("error") or reason
    data["runner_failure"] = reason
    _write_json(rj, data)
    if not (run_dir / "timing.json").exists():
        _write_json(run_dir / "timing.json", {"total_tokens": None, "duration_ms": 0,
                                              "total_duration_seconds": 0.0, "usage": None,
                                              "provenance": "wall_clock"})
    if not (run_dir / "stream.jsonl").exists():
        (run_dir / "stream.jsonl").write_text("", encoding="utf-8")
    if not (run_dir / "transcript.md").exists():
        text = f"## Eval Prompt\n\n{ev['prompt']}\n\n## Result\n\n{reason}\n"
        (run_dir / "transcript.md").write_text(text, encoding="utf-8")
        if not (run_dir / "metrics.json").exists():
            _write_json(run_dir / "metrics.json", {
                "tool_calls": {}, "total_tool_calls": 0, "total_steps": 0, "files_created": [],
                "errors_encountered": 0, "output_chars": 0, "transcript_chars": len(text)})


def execute_run(plan: Plan, ev: dict, config: str, run_dir: Path, budget: claude_cli.Budget) -> dict:
    """Run one session and write all of its artifacts. Raises BudgetExceeded
    or ShuttingDown (nothing written) when the run was never launched.

    stream.jsonl, run.json and timing.json are written the moment the call
    returns, before outputs are copied, so a copy failure cannot lose them.
    """
    if not budget.may_launch():
        raise claude_cli.BudgetExceeded("cap already crossed")
    call = _build_call(plan, ev["prompt"])
    if config == WITH_SKILL:
        stage = (plan.skill_name, plan.skill_dir)
    elif plan.baseline_kind == "snapshot":
        stage = (plan.baseline_name, plan.baseline_dir)
    else:
        stage = (None, None)

    started = _now()
    with claude_cli.temp_project(stage[0], source_dir=stage[1]) as project:
        (project / "outputs").mkdir()
        for rel in ev.get("files", []):
            src = plan.skill_root / rel
            dest = project / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)
        outcome = claude_cli.run_and_parse(
            call, project, timeout=RUN_TIMEOUT_S, budget=budget)
        ended = _now()
        summary, raw = outcome.summary, outcome.raw

        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "stream.jsonl").write_text(raw.stdout, encoding="utf-8")

        if summary.result is not None:
            duration_ms = summary.duration_ms
            if duration_ms is None:
                duration_ms = int(raw.duration_s * 1000)
            timing = {
                "total_tokens": summary.total_tokens,
                "duration_ms": duration_ms,
                "total_duration_seconds": round(duration_ms / 1000, 1),
                "usage": {k: int((summary.usage or {}).get(k) or 0) for k in (
                    "input_tokens", "output_tokens",
                    "cache_creation_input_tokens", "cache_read_input_tokens")},
                "provenance": "cli_result",
            }
        else:
            duration_ms = int(raw.duration_s * 1000)
            timing = {"total_tokens": None, "duration_ms": duration_ms,
                      "total_duration_seconds": round(duration_ms / 1000, 1),
                      "usage": None, "provenance": "wall_clock"}
        timing["executor_start"] = started
        timing["executor_end"] = ended
        timing["executor_duration_seconds"] = round(raw.duration_s, 1)
        _write_json(run_dir / "timing.json", timing)

        skills = summary.init.get("skills") if summary.init is not None else None
        skills = list(skills) if isinstance(skills, list) else None
        contaminated = _is_contaminated(plan, config, skills)
        result = summary.result or {}
        run_json = {
            "configuration": config,
            "exit_code": raw.returncode,
            "stderr_tail": raw.stderr_tail,
            "is_error": summary.is_error,
            "terminal_reason": summary.terminal_reason,
            "permission_denials": list(result.get("permission_denials") or []),
            "permission_denied_events": sum(1 for d in summary.denials if d.get("source") == "event"),
            "init": {
                "skills": skills,
                "model": summary.model,
                "tools": summary.tools if summary.init is not None else None,
                "permission_mode": (summary.init or {}).get("permissionMode"),
            },
            "cli_version": summary.claude_code_version,
            "argv": list(call.argv),
            "extra_args": list(call.extra_args),
            "permission_mode": _argv_value(call.argv, "--permission-mode"),
            "added_flags": list(call.additions),
            "total_cost_usd": summary.total_cost_usd,
            "num_turns": summary.num_turns,
            "contaminated": contaminated,
            "status": "contaminated" if contaminated else outcome.status,
            "error": outcome.error,
            "timed_out": raw.timed_out,
            "warnings": list(call.warnings),
        }
        if config == WITHOUT_SKILL and skills is None and run_json["status"] != "error":
            # No inventory means no evidence the baseline could not see the candidate.
            run_json["status"] = "error"
            run_json["error"] = "init.skills unavailable; baseline isolation could not be verified"
        _write_json(run_dir / "run.json", run_json)

        # Copy while the project still exists. A failure here is a run error,
        # recorded in run.json; the artifacts above are already on disk.
        try:
            shutil.copytree(project / "outputs", run_dir / "outputs", dirs_exist_ok=True)
        except (OSError, shutil.Error) as e:
            (run_dir / "outputs").mkdir(exist_ok=True)
            run_json["output_copy_error"] = f"{type(e).__name__}: {e}"
            run_json["error"] = run_json["error"] or "outputs could not be copied"
            if run_json["status"] != "contaminated":
                run_json["status"] = "error"
            _write_json(run_dir / "run.json", run_json)

    transcript, partial = render_stream(raw.stdout, ev["prompt"])
    (run_dir / "transcript.md").write_text(transcript, encoding="utf-8")
    files, out_chars = _output_stats(run_dir / "outputs")
    metrics = {
        "tool_calls": partial["tool_calls"],
        "total_tool_calls": sum(partial["tool_calls"].values()),
        "total_steps": partial["steps"],
        "files_created": files,
        "errors_encountered": partial["errors"],
        "output_chars": out_chars,
        "transcript_chars": len(transcript),
    }
    _write_json(run_dir / "metrics.json", metrics)

    return {"run_dir": str(run_dir), "status": run_json["status"], "error": run_json["error"],
            "cost_usd": outcome.cost_usd}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--evals", "evals_path", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Path to evals.json.")
@click.option("--skill-path", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Candidate skill directory (holds SKILL.md).")
@click.option("--workspace", required=True, type=click.Path(file_okay=False, path_type=Path),
              help="Iteration directory to write eval-*/ into.")
@click.option("--baseline", default="no_skill", show_default=True,
              help="`no_skill`, or the path of a snapshot skill directory.")
@click.option("--runs", default=1, show_default=True, type=click.IntRange(min=1), help="Runs per eval x configuration.")
@click.option("--model", default=None, help="Model id; omitted unless given. The actual model is recorded.")
@click.option("--tools", default=None, help="Tool list; default is the execution set, with Skill always included.")
@click.option("--permission-mode", default=None, help="Permission mode; default acceptEdits.")
@click.option("--max-total-usd", default=None, type=click.FloatRange(min=0), help="Stop launching new runs once cumulative cost crosses this.")
@click.option("--yes", is_flag=True, help="Confirm and spend. Without it, print the plan and make no calls.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.argument("extra", nargs=-1, type=click.UNPROCESSED)
def main(evals_path, skill_path, workspace, baseline, runs, model, tools, permission_mode,
         max_total_usd, yes, as_json, extra):
    """Run every eval x configuration x run as an isolated `claude -p` session.

    Anything after a literal `--` is passed to `claude -p` verbatim, e.g.

      run_tasks.py --evals e.json --skill-path s --workspace w --yes -- --allowedTools 'Bash(uv run *)'
    """
    claude_cli.sweep_stale()
    skill_dir = skill_path.resolve()
    skill_name = _skill_name(skill_dir, "candidate")
    evals = _load_evals(evals_path, skill_dir)

    if baseline == "no_skill":
        baseline_kind, baseline_dir, baseline_name = "no_skill", None, None
        baseline_label = "no skill"
    else:
        baseline_dir = Path(baseline).resolve()
        if not baseline_dir.is_dir():
            raise _fail(f"--baseline must be `no_skill` or a skill directory; {baseline} is neither")
        if baseline_dir.samefile(skill_dir):
            raise _fail("--baseline is the same directory as --skill-path; "
                        "a snapshot must be a separate copy of the old skill")
        baseline_kind = "snapshot"
        baseline_name = _skill_name(baseline_dir, "baseline")
        baseline_label = f"snapshot: {baseline_dir.name}"

    plan = Plan(skill_dir=skill_dir, skill_name=skill_name, baseline_kind=baseline_kind,
                baseline_dir=baseline_dir, baseline_name=baseline_name, model=model, tools=tools,
                permission_mode=permission_mode, extra_args=list(extra), skill_root=skill_dir)
    try:
        claude_cli.validate_passthrough(plan.extra_args)
        probe = _build_call(plan, evals[0]["prompt"])
        budget = claude_cli.Budget(max_total_usd)
    except (claude_cli.PassthroughError, ValueError) as e:
        raise _fail(str(e))

    workspace = workspace.resolve()
    for label, src in (("--skill-path", skill_dir), ("--baseline", baseline_dir)):
        if src is not None and (workspace == src or workspace.is_relative_to(src)):
            raise _fail(f"--workspace must not be {label} or inside it; the workspace is never written into a skill's source tree")
    if conflict := validation.skill_tree_conflict(workspace):
        raise _fail(f"--workspace must not be inside a skill or a skills directory ({conflict}); "
                    "put it in the current directory, e.g. ./<skill-name>-workspace/iteration-1")
    eval_dirs = {ev["id"]: workspace / f"eval-{ev['id']}-{_slug(ev['name'])}" for ev in evals}
    total = len(evals) * 2 * runs
    # Interleave so no configuration runs ahead of the other.
    jobs = build_jobs(evals, runs, eval_dirs)
    clash = [str(j[3]) for j in jobs if j[3].exists()]
    if clash:
        raise _fail("run directories already exist (use a new iteration directory): " + ", ".join(clash[:3]))

    plan_info = {
        "evals": len(evals), "configurations": 2, "runs": runs, "calls": total,
        "baseline_kind": baseline_kind, "baseline_label": baseline_label,
        "workspace": str(workspace), "max_total_usd": max_total_usd,
        "warnings": probe.warnings,
    }
    if not yes:
        cap = f"cap ${max_total_usd:.2f}" if max_total_usd is not None else "no --max-total-usd cap set"
        msg = (f"Plan: {len(evals)} evals x 2 configurations x {runs} runs = {total} claude -p calls.\n"
               f"Each call is a full agent session that spends model usage ({cap}).\n"
               f"Nothing was run. Re-run with --yes to start.")
        if as_json:
            click.echo(json.dumps({**plan_info, "ran": False}, indent=2))
        else:
            click.echo(msg)
        return

    if shutil.which("claude") is None:
        raise _fail("`claude` was not found on PATH")
    claude_cli.install_signal_handlers()

    for ev in evals:
        d = eval_dirs[ev["id"]]
        d.mkdir(parents=True, exist_ok=True)
        meta = {
            "eval_id": ev["id"], "eval_name": ev["name"], "prompt": ev["prompt"],
            "expected_output": ev.get("expected_output", ""),
            "files": list(ev.get("files", [])),
            "expectations": list(ev.get("expectations", [])),
            "baseline_kind": baseline_kind, "baseline_label": baseline_label,
        }
        (d / "eval_metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    records: list[dict] = []
    skipped = 0
    failures: list[str] = []
    lock = threading.Lock()

    def work(job):
        nonlocal skipped
        ev, config, k, run_dir = job
        try:
            rec = execute_run(plan, ev, config, run_dir, budget)
            rec.update(eval_id=ev["id"], configuration=config, run=k)
            with lock:
                records.append(rec)
        except (claude_cli.BudgetExceeded, claude_cli.ShuttingDown):
            with lock:
                skipped += 1
        except Exception as e:  # one broken run must not lose the others
            try:
                record_failure(config, ev, run_dir, e)
            except Exception:
                pass  # reported below either way
            with lock:
                failures.append(f"eval {ev['id']} {config} run-{k}: {type(e).__name__}: {e}")

    pool = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, total))
    try:
        for f in [pool.submit(work, j) for j in jobs]:
            f.result()
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)

    out = {**plan_info, "ran": True, "recorded": len(records), "skipped_by_budget": skipped,
           "spent_usd": round(budget.spent, 6), "failures": failures,
           "runs_detail": sorted(records, key=lambda r: r["run_dir"])}
    if as_json:
        click.echo(json.dumps(out, indent=2))
    else:
        by_status: dict[str, int] = {}
        for r in records:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        click.echo(f"Recorded {len(records)} of {total} runs {by_status}; spent ${budget.spent:.4f}.")
        if skipped:
            click.echo(f"{skipped} runs not launched: --max-total-usd cap crossed or shutdown.")
        for line in failures:
            click.echo(f"FAILED {line}", err=True)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()

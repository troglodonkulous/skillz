#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["click", "pyyaml"]
# ///
"""Trigger trial engine: does a description make Claude invoke the skill?

Library (run_loop.py imports it) and standalone CLI. Every trial runs
`claude -p` through claude_cli in its own temp project holding the real skill
at `.claude/skills/<name>/` with its full frontmatter and only `description`
substituted, so concurrent trials never see each other's skills.

Library API (import by bare name after putting scripts/eval on sys.path)
------------------------------------------------------------------------
    load_eval_set(path) -> list[dict]
        Reads the trigger eval set; assigns a missing `id` (q1, q2, ... in
        file order, skipping taken ids). Results are keyed by id, never by
        query text, so duplicate query strings stay independent.
    with_description(skill_md_text, new_description) -> str
        Replaces only `description` in the frontmatter. Every other key, the
        key order and the body are preserved byte-for-byte. The single
        implementation; run_loop.py reuses it.
    check_runnable(skill_md_text) -> (frontmatter, body)
        Raises RefusedSkill (reason stated) for `disable-model-invocation:
        true`, and ValueError for unusable frontmatter. Call before any CLI call.
    collect_listing_settings(frontmatter) -> dict
        The effective listing settings (spec 2.4), for results.json
        `listing_settings`.
    plan_calls(n_queries, runs_per_query, fixed_runs) -> dict
        {"canary", "worst_case", "expected_min"}; worst_case includes the canary.
    preflight_canary(model, *, extra_args=(), timeout=60, budget=None) -> CanaryResult
        Runs a uniquely named canary skill and requires an exact trigger.
        Raises CanaryFailed (message carries CLI version, exit code, stderr)
        on no match, non-zero exit, nesting refusal, or a missing `claude`.
        CanaryResult: version, cost_usd, call (dict like a calls[] entry).
    evaluate_description(eval_set, *, skill_name, skill_md, description, model,
                         skill_dir=None, runs_per_query=3, fixed_runs=False,
                         threshold=0.5, num_workers=5, timeout=60,
                         extra_args=(), budget=None) -> EvalResult
        skill_md is the original SKILL.md text; skill_dir (optional) is
        copied whole into each trial project. EvalResult: per_query (schemas.md
        `per_query[]`, in eval-set order), summary {total, passed, failed,
        errors}, error_runs, error_queries, calls (list of dicts: kind, query_id,
        argv, extra_args, exit_code, total_cost_usd), cost_usd, budget_stopped.
    Budget / BudgetExceeded come from claude_cli; pass one Budget to share
    the --max-total-usd cap across the canary and every evaluation.

Adaptive runs (spec 2.4): two non-error runs; a third only when they
disagree. An `error` run never counts toward agreement. A query with fewer
than two non-error outcomes is `error`. `runs_per_query` is the maximum;
`fixed_runs` always uses it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import claude_cli as cc  # noqa: E402
import validation  # noqa: E402

TRIAL_LISTING_NOTE = (
    "default: --setting-sources project, ~20 skills listed; "
    "may be kinder than a crowded real session"
)
MIN_RUNS = 2


class RefusedSkill(ValueError):
    """The skill cannot be optimized; the message states why."""


class CanaryFailed(RuntimeError):
    """The preflight canary did not trigger; the message has version, exit code, stderr."""


# --------------------------------------------------------------------------
# Eval set
# --------------------------------------------------------------------------

def load_eval_set(path: Path | str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("eval set must be a non-empty JSON array")
    taken: set[str] = set()
    for i, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ValueError(f"eval set entry {i} is not an object")
        if not isinstance(item.get("query"), str) or not item["query"]:
            raise ValueError(f"eval set entry {i}: `query` must be a non-empty string")
        if not isinstance(item.get("should_trigger"), bool):
            raise ValueError(f"eval set entry {i}: `should_trigger` must be true or false")
        if "id" in item:
            if not isinstance(item["id"], str) or not item["id"]:
                raise ValueError(f"eval set entry {i}: `id` must be a non-empty string")
            if item["id"] in taken:
                raise ValueError(f"eval set entry {i}: duplicate id {item['id']!r}")
            taken.add(item["id"])
    out = []
    counter = 0
    for item in data:
        item = dict(item)
        if "id" not in item:
            counter += 1
            while f"q{counter}" in taken:
                counter += 1
            item["id"] = f"q{counter}"
            taken.add(item["id"])
        out.append(item)
    return out


# --------------------------------------------------------------------------
# SKILL.md handling
# --------------------------------------------------------------------------

def _frontmatter_span(lines: list[str]) -> tuple[int, int]:
    """(first line index after the opening ---, index of the closing ---)."""
    if not lines or lines[0].strip() != "---":
        raise ValueError("No YAML frontmatter found")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ValueError("Invalid frontmatter format: no closing '---'")
    return 1, end


def with_description(skill_md_text: str, new_description: str) -> str:
    """Return skill_md_text with only `description` replaced (see module docstring)."""
    bom = "﻿" if skill_md_text.startswith("﻿") else ""
    text = skill_md_text[len(bom):]
    lines = text.splitlines(keepends=True)
    start, end = _frontmatter_span(lines)
    key = next((i for i in range(start, end) if re.match(r"description\s*:", lines[i])), None)
    if key is None:
        raise ValueError("SKILL.md frontmatter has no top-level `description`")
    stop = key + 1
    while stop < end and (not lines[stop].strip() or lines[stop][0] in " \t"):
        stop += 1
    while stop > key + 1 and not lines[stop - 1].strip():
        stop -= 1  # blank lines before the next key stay where they were
    eol = "\r\n" if lines[key].endswith("\r\n") else "\n"
    # A JSON string is a valid double-quoted YAML scalar, whatever the content.
    new_line = f"description: {json.dumps(new_description, ensure_ascii=False)}{eol}"
    result = bom + "".join(lines[:key]) + new_line + "".join(lines[stop:])

    old_fm, _, _ = validation.parse_frontmatter(skill_md_text)
    new_fm, new_body, err = validation.parse_frontmatter(result)
    _, old_body, _ = validation.parse_frontmatter(skill_md_text)
    expected = dict(old_fm or {})
    expected["description"] = new_description
    if err or new_fm != expected or list(new_fm) != list(expected) or new_body != old_body:
        raise ValueError(f"could not substitute the description safely: {err or 'frontmatter changed'}")
    return result


def check_runnable(skill_md_text: str) -> tuple[dict, str]:
    """Parse the skill and refuse one that can never auto-trigger."""
    fm, body, err = validation.parse_frontmatter(skill_md_text)
    if fm is None:
        raise ValueError(err)
    flag = fm.get("disable-model-invocation")
    if flag is True or (isinstance(flag, str) and flag.strip().lower() == "true"):
        raise RefusedSkill(
            "Refusing to optimize: `disable-model-invocation: true` means Claude never "
            "auto-triggers this skill, so a description cannot change how often it triggers."
        )
    if not isinstance(fm.get("name"), str) or not fm["name"].strip():
        raise ValueError("SKILL.md frontmatter needs a `name`")
    if not isinstance(fm.get("description"), str) or not fm["description"].strip():
        raise ValueError("SKILL.md frontmatter needs a non-empty `description`")
    return fm, body


def collect_listing_settings(frontmatter: dict) -> dict:
    """The effective skill-listing settings, each None when unset."""
    settings: dict = {}
    path = Path.home() / ".claude" / "settings.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            settings = loaded
    except (OSError, ValueError):
        pass
    overrides = settings.get("skillOverrides")
    entry = overrides.get(frontmatter.get("name")) if isinstance(overrides, dict) else None
    when_to_use = frontmatter.get("when_to_use")
    return {
        "SLASH_COMMAND_TOOL_CHAR_BUDGET": os.environ.get("SLASH_COMMAND_TOOL_CHAR_BUDGET"),
        "skillListingBudgetFraction": settings.get("skillListingBudgetFraction"),
        "skillListingMaxDescChars": settings.get("skillListingMaxDescChars"),
        "skillOverrides": entry,
        "trial_listing": TRIAL_LISTING_NOTE,
        "description_chars": len(frontmatter.get("description") or ""),
        "when_to_use_chars": len(when_to_use) if isinstance(when_to_use, str) else 0,
    }


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------

def plan_calls(n_queries: int, runs_per_query: int, fixed_runs: bool) -> dict:
    expected_min = n_queries * (runs_per_query if fixed_runs else MIN_RUNS) + 1
    return {"canary": 1, "worst_case": n_queries * runs_per_query + 1, "expected_min": expected_min}


# --------------------------------------------------------------------------
# Canary
# --------------------------------------------------------------------------

@dataclass
class CanaryResult:
    version: str | None
    cost_usd: float
    call: dict


def _call_record(call: cc.ClaudeCall, outcome: cc.RunOutcome, **extra) -> dict:
    return {
        **extra,
        "kind": extra.get("kind", call.kind),
        "argv": list(call.argv),
        "extra_args": list(call.extra_args),
        "exit_code": outcome.raw.returncode,
        "total_cost_usd": outcome.summary.total_cost_usd,
    }


def preflight_canary(model: str, *, extra_args=(), timeout: float = 60,
                     budget: cc.Budget | None = None) -> CanaryResult:
    token = uuid.uuid4().hex[:10]
    name = f"skillz-canary-{token}"
    skill_md = (
        f"---\nname: {name}\n"
        f"description: Use whenever the user message contains the code word {token}. "
        f"It only acknowledges the code word.\n---\n\n# Canary\n\nReply with the code word.\n"
    )
    call = cc.build_argv(cc.TRIGGER, f"The code word is {token}.", model=model,
                         extra_args=extra_args)
    try:
        with cc.temp_project(name, skill_md=skill_md) as project:
            outcome = cc.run_and_parse(call, project, timeout=timeout, skill_name=name,
                                       budget=budget)
    except (cc.ClaudeNotFound, cc.BudgetExceeded) as e:
        raise CanaryFailed(f"Preflight canary could not run: {e}") from e
    version = outcome.summary.claude_code_version or cc.cli_version()
    record = _call_record(call, outcome, kind="canary")
    if outcome.status != "triggered":
        why = outcome.error or "the canary skill was not invoked (exact match required)"
        raise CanaryFailed(
            "Preflight canary failed; no real query was run.\n"
            f"  reason: {why}\n  CLI version: {version}\n"
            f"  exit code: {outcome.raw.returncode}\n  stderr: {outcome.raw.stderr_tail.strip() or '(empty)'}"
        )
    return CanaryResult(version, outcome.cost_usd, record)


# --------------------------------------------------------------------------
# Trials
# --------------------------------------------------------------------------

@dataclass
class EvalResult:
    per_query: list[dict]
    summary: dict
    error_runs: int
    error_queries: int
    calls: list[dict]
    cost_usd: float
    budget_stopped: bool = False


def _score_query(item: dict, outcomes: list[dict], threshold: float) -> dict:
    triggered = sum(o["outcome"] == "triggered" for o in outcomes)
    measured = triggered + sum(o["outcome"] == "not_triggered" for o in outcomes)
    rate = triggered / measured if measured else None
    if measured < MIN_RUNS:
        result = "error"
    elif item["should_trigger"]:
        result = "pass" if rate >= threshold else "fail"
    else:
        result = "pass" if rate < threshold else "fail"
    return {
        "id": item["id"], "query": item["query"], "should_trigger": item["should_trigger"],
        "outcomes": outcomes, "runs_used": len(outcomes),
        "trigger_rate": rate, "result": result,
    }


def _needs_another_run(outcomes: list[dict], max_runs: int, fixed: bool) -> bool:
    if len(outcomes) >= max_runs:
        return False
    if fixed:
        return True
    measured = [o["outcome"] for o in outcomes if o["outcome"] != "error"]
    if len(measured) < MIN_RUNS:
        return True
    return measured[0] != measured[1] and len(measured) < 3


def evaluate_description(
    eval_set: list[dict], *, skill_name: str, skill_md: str, description: str, model: str,
    skill_dir: Path | str | None = None, runs_per_query: int = 3, fixed_runs: bool = False,
    threshold: float = 0.5, num_workers: int = 5, timeout: float = 60, extra_args=(),
    budget: cc.Budget | None = None,
) -> EvalResult:
    if runs_per_query < MIN_RUNS:
        raise ValueError(f"runs_per_query must be at least {MIN_RUNS}")
    cc.validate_passthrough(extra_args)
    staged_md = with_description(skill_md, description)
    lock = threading.Lock()
    calls: list[dict] = []
    stopped = threading.Event()

    def one_run(item: dict) -> dict | None:
        call = cc.build_argv(cc.TRIGGER, item["query"], model=model, extra_args=extra_args)
        try:
            with cc.temp_project(skill_name, source_dir=skill_dir, skill_md=staged_md) as project:
                out = cc.run_and_parse(call, project, timeout=timeout, skill_name=skill_name,
                                       budget=budget)
        except cc.BudgetExceeded:
            stopped.set()
            return None
        with lock:
            calls.append(_call_record(call, out, kind="trigger", query_id=item["id"]))
        record = {"outcome": out.status, "exit_code": out.raw.returncode,
                  "stderr_tail": out.raw.stderr_tail or None}
        if out.status == "error":
            record["reason"] = out.error
        return record

    def run_query(item: dict) -> dict:
        outcomes: list[dict] = []
        while _needs_another_run(outcomes, runs_per_query, fixed_runs):
            record = one_run(item)
            if record is None:
                break
            outcomes.append(record)
        return _score_query(item, outcomes, threshold)

    with ThreadPoolExecutor(max_workers=max(1, num_workers)) as pool:
        per_query = list(pool.map(run_query, eval_set))

    passed = sum(q["result"] == "pass" for q in per_query)
    failed = sum(q["result"] == "fail" for q in per_query)
    errors = sum(q["result"] == "error" for q in per_query)
    return EvalResult(
        per_query=per_query,
        summary={"total": len(per_query), "passed": passed, "failed": failed, "errors": errors},
        error_runs=sum(o["outcome"] == "error" for q in per_query for o in q["outcomes"]),
        error_queries=errors,
        calls=calls,
        cost_usd=sum(c["total_cost_usd"] or 0.0 for c in calls),
        budget_stopped=stopped.is_set(),
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_plan(n_queries, runs, fixed, plan, model, passthrough) -> list[str]:
    lines = [
        f"Trigger evaluation plan: {n_queries} queries, model {model}",
        f"  runs per query: {'exactly' if fixed else 'up to'} {runs}"
        + ("" if fixed else " (2, a third only when they disagree)"),
        f"  worst case: {plan['worst_case']} claude -p calls, including {plan['canary']} canary",
        f"  expected minimum: {plan['expected_min']} calls",
    ]
    if passthrough:
        lines.append(f"  passthrough: {' '.join(passthrough)}")
    lines.append("No calls were made. Re-run with --yes to start.")
    return lines


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--eval-set", "eval_set_path", required=True, type=click.Path(exists=True, dir_okay=False),
              help="Trigger eval set JSON.")
@click.option("--skill-path", required=True, type=click.Path(exists=True, file_okay=False),
              help="Skill directory containing SKILL.md.")
@click.option("--model", required=True, help="Model for claude -p.")
@click.option("--description", default=None, help="Test this description instead of the skill's own.")
@click.option("--runs-per-query", default=3, show_default=True, type=int,
              help="Maximum runs per query (exactly this many with --fixed-runs).")
@click.option("--fixed-runs", is_flag=True, help="Always use --runs-per-query runs, not adaptive.")
@click.option("--num-workers", default=5, show_default=True, type=int, help="Parallel queries.")
@click.option("--timeout", default=60, show_default=True, type=int, help="Seconds per claude call.")
@click.option("--max-total-usd", default=None, type=float,
              help="Stop launching calls once cumulative total_cost_usd crosses this.")
@click.option("--yes", is_flag=True, help="Confirm the plan and spend. Without it, print the plan and exit.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.argument("passthrough", nargs=-1, type=click.UNPROCESSED)
def main(eval_set_path, skill_path, model, description, runs_per_query, fixed_runs, num_workers,
         timeout, max_total_usd, yes, as_json, passthrough):
    """Measure whether a skill description triggers. Extra `claude` flags go after `--`."""
    skill_dir = Path(skill_path)
    md_path = skill_dir / "SKILL.md"
    if not md_path.is_file():
        raise click.ClickException(f"No SKILL.md found at {skill_dir}")
    skill_md = md_path.read_text(encoding="utf-8")
    try:
        fm, _ = check_runnable(skill_md)  # refusal comes before any CLI call
        if runs_per_query < MIN_RUNS:
            raise ValueError(f"--runs-per-query must be at least {MIN_RUNS}")
        eval_set = load_eval_set(eval_set_path)
        cc.validate_passthrough(passthrough)
        budget = cc.Budget(max_total_usd)
    except (ValueError, json.JSONDecodeError) as e:
        raise click.ClickException(str(e))

    name = fm["name"]
    tested = description if description is not None else fm["description"]
    try:
        with_description(skill_md, tested)
    except ValueError as e:
        raise click.ClickException(str(e))
    plan = plan_calls(len(eval_set), runs_per_query, fixed_runs)

    if not yes:
        if as_json:
            click.echo(json.dumps({"plan": plan, "calls_made": 0, "confirmed": False}, indent=2))
        else:
            click.echo("\n".join(_print_plan(len(eval_set), runs_per_query, fixed_runs, plan, model, passthrough)))
        return

    cc.sweep_stale()
    try:
        canary = preflight_canary(model, extra_args=passthrough, timeout=timeout, budget=budget)
    except CanaryFailed as e:
        click.echo(str(e), err=True)
        sys.exit(3)
    remaining = plan["worst_case"] - 1
    click.echo(f"Canary passed (CLI {canary.version}). Estimated remaining cost at most "
               f"${canary.cost_usd * remaining:.2f} for up to {remaining} calls.", err=True)

    res = evaluate_description(
        eval_set, skill_name=name, skill_md=skill_md, description=tested, model=model,
        skill_dir=skill_dir, runs_per_query=runs_per_query, fixed_runs=fixed_runs,
        num_workers=num_workers, timeout=timeout, extra_args=passthrough, budget=budget)

    output = {
        "skill_name": name,
        "cli_version": canary.version,
        "description": tested,
        "listing_settings": collect_listing_settings({**fm, "description": tested}),
        "settings": {"runs_per_query": runs_per_query, "fixed_runs": fixed_runs,
                     "threshold": 0.5, "model": model},
        "calls": {"planned_worst_case": plan["worst_case"], "made": len(res.calls) + 1, "canary": 1},
        "call_log": [canary.call, *res.calls],
        "total_cost_usd": canary.cost_usd + res.cost_usd,
        "error_counts": {"error_runs": res.error_runs, "error_queries": res.error_queries},
        "budget_stopped": res.budget_stopped,
        "results": res.per_query,
        "summary": res.summary,
    }
    if as_json:
        click.echo(json.dumps(output, indent=2))
        return
    s = res.summary
    click.echo(f"{s['passed']}/{s['total']} passed, {s['failed']} failed, {s['errors']} error")
    for q in res.per_query:
        rate = "n/a" if q["trigger_rate"] is None else f"{q['trigger_rate']:.2f}"
        click.echo(f"  [{q['result'].upper()}] {q['id']} rate={rate} runs={q['runs_used']} "
                   f"expected={q['should_trigger']}: {q['query'][:70]}")
    if res.error_runs:
        click.echo(f"WARNING: {res.error_runs} error runs ({res.error_queries} queries reported error); "
                   "errors are not non-triggers.", err=True)
    if res.budget_stopped:
        click.echo("max-total-usd reached: later calls were not launched.", err=True)


if __name__ == "__main__":
    main()

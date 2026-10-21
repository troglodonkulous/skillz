#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["click", "pyyaml"]
# ///
"""Optimize Triggers: measure a skill description, rewrite it from train
failures, and report the best candidate by held-out test score.

Builds on run_eval.py (canary, adaptive trials, listing settings) and
improve_description.py (tool-free rewrite). It reports `best_description` and
never applies it: the skill, commands and settings stay untouched (spec 4.4).

Loop (spec 2.4)
    1. Canary (aborts the run if it fails).
    2. Iteration 1 scores the ORIGINAL description on train and on test.
    3. Each later iteration rewrites from the previous train failures only
       (never test queries), then scores train.
    4. Test is scored only when train >= the best train score so far;
       otherwise `test: "skipped (train regressed)"` and the candidate can
       never be selected.
    5. Stops early when every train query passes. Default 3 iterations.
    6. best_description = highest held-out test score among tested
       candidates (earliest wins a tie). With --holdout 0 it is the highest
       train score.

Without --yes the plan and budget are printed and no `claude` call is made.
Results go to <skill-name>-workspace/trigger-opt/<timestamp>/ under the cwd
(results.json, report.html, logs/) or --results-dir. Trial projects live in
system temp and are removed on exit and on crash.
"""

from __future__ import annotations

import json
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import claude_cli as cc  # noqa: E402
import generate_report  # noqa: E402
import improve_description as imp  # noqa: E402
import run_eval  # noqa: E402
import validation  # noqa: E402

SKIPPED = "skipped (train regressed)"
SELECTION_METHOD = "highest held-out test score among tested candidates"
TRAIN_ONLY_METHOD = "highest train score (no held-out set: --holdout 0)"
ADAPTIVE_EXPECTED_FACTOR = 0.75  # spec 2.4: adaptive runs save roughly 25-30% of trial calls
SPLIT_SEED = 42


# --------------------------------------------------------------------------
# Split and plan
# --------------------------------------------------------------------------

def split_eval_set(eval_set: list[dict], holdout: float, seed: int = SPLIT_SEED) -> tuple[list[dict], list[dict]]:
    """Seeded split by query `id`, stratified by should_trigger (criterion 27).

    Duplicate query strings have distinct ids, so each is placed on its own.
    A stratum of one item stays in train.
    """
    if holdout <= 0:
        return list(eval_set), []
    rng = random.Random(seed)
    test_ids: set[str] = set()
    for polarity in (True, False):
        group = [e for e in eval_set if e["should_trigger"] is polarity]
        rng.shuffle(group)
        n_test = max(1, int(len(group) * holdout)) if len(group) >= 2 else 0
        test_ids.update(e["id"] for e in group[:n_test])
    train = [e for e in eval_set if e["id"] not in test_ids]
    test = [e for e in eval_set if e["id"] in test_ids]
    return train, test


def plan_loop(n_total: int, n_train: int, runs: int, fixed: bool, iterations: int) -> dict:
    """Call counts. worst_case = queries x max runs x iterations + canary + rewrites."""
    per_full_eval = run_eval.plan_calls(n_total, runs, fixed)["worst_case"] - 1  # drops the canary
    rewrites = iterations - 1
    worst = per_full_eval * iterations + 1 + rewrites
    min_runs = runs if fixed else run_eval.MIN_RUNS
    low = n_total * min_runs + (iterations - 1) * n_train * min_runs + 1 + rewrites
    high = worst if fixed else math.ceil(ADAPTIVE_EXPECTED_FACTOR * per_full_eval * iterations) + 1 + rewrites
    return {"canary": 1, "rewrites": rewrites, "worst_case": worst,
            "expected_min": low, "expected_max": max(low, min(high, worst))}


def plan_lines(plan: dict, n_total: int, n_train: int, n_test: int, runs: int, fixed: bool,
               iterations: int, model: str, passthrough) -> list[str]:
    lines = [
        f"Trigger optimization plan: {n_total} queries ({n_train} train, {n_test} test), "
        f"{iterations} iterations, model {model}",
        f"  runs per query: {'exactly' if fixed else 'up to'} {runs}"
        + ("" if fixed else " (2, a third only when they disagree)"),
        f"  worst case: {n_total} queries x {runs} runs x {iterations} iterations "
        f"+ {plan['canary']} canary + {plan['rewrites']} rewrites = {plan['worst_case']} claude -p calls",
        f"  expected: about {plan['expected_min']}-{plan['expected_max']} calls "
        "(fewer if every train query passes early)",
    ]
    if passthrough:
        lines.append(f"  passthrough: {' '.join(passthrough)}")
    return lines


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _block(per_query: list[dict]) -> dict:
    count = lambda r: sum(q["result"] == r for q in per_query)  # noqa: E731
    return {"passed": count("pass"), "failed": count("fail"), "errors": count("error"),
            "total": len(per_query), "per_query": per_query}


def _score(block: dict) -> str:
    return f"{block['passed']}/{block['total']}"


def select_best(iterations: list[dict], has_test: bool) -> dict | None:
    """First iteration with the top held-out score; skipped or incomplete ones never qualify."""
    cands = []
    for it in iterations:
        if it.get("incomplete"):
            continue
        scored = it["test"] if has_test else it["train"]
        if isinstance(scored, dict):
            cands.append((scored["passed"], it))
    return max(cands, key=lambda c: c[0])[1] if cands else None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_dir_collision(passthrough, skill_name: str) -> str | None:
    """An --add-dir that already holds a skill of this name could be credited as the candidate."""
    for d in cc.add_dir_values(passthrough):
        if (Path(d) / ".claude" / "skills" / skill_name).exists():
            return d
    return None


def _claim_default_dir(wanted: Path) -> Path:
    """Create the timestamped default dir exclusively; a same-second run gets a -2, -3, ... suffix."""
    wanted.parent.mkdir(parents=True, exist_ok=True)
    for n in range(1, 1000):
        candidate = wanted if n == 1 else wanted.with_name(f"{wanted.name}-{n}")
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise click.ClickException(f"could not allocate a results directory under {wanted.parent}")


def _write_results(results_dir: Path, results: dict) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    (results_dir / "report.html").write_text(generate_report.generate_html(results), encoding="utf-8")


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------

def run_loop(
    *, eval_set: list[dict], skill_dir: Path, skill_md: str, frontmatter: dict, model: str,
    max_iterations: int, runs_per_query: int, fixed_runs: bool, holdout: float, num_workers: int,
    timeout: float, extra_args, budget: cc.Budget, results_dir: Path, verbose: bool = False,
) -> dict:
    name = frontmatter["name"]
    original = frontmatter["description"]
    when_to_use = frontmatter.get("when_to_use")
    when_chars = len(when_to_use) if isinstance(when_to_use, str) else 0
    train_set, test_set = split_eval_set(eval_set, holdout)
    has_test = bool(test_set)
    train_ids = {e["id"] for e in train_set}
    plan = plan_loop(len(eval_set), len(train_set), runs_per_query, fixed_runs, max_iterations)
    log_dir = results_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        with (log_dir / "run.log").open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
        if verbose:
            click.echo(msg, err=True)

    all_calls: list[dict] = []
    iterations: list[dict] = []
    results: dict = {
        "skill_name": name,
        "cli_version": cc.cli_version() or "unknown",
        "started_at": _now(),
        "exit_reason": "in progress",
        "original_description": original,
        "best_description": None,
        "best_selection": None,
        "listing_settings": run_eval.collect_listing_settings(frontmatter),
        "listing_warnings": [],
        "settings": {"runs_per_query": runs_per_query, "fixed_runs": fixed_runs,
                     "max_iterations": max_iterations, "holdout": holdout, "threshold": 0.5,
                     "model": model},
        "calls": {"planned_worst_case": plan["worst_case"], "made": 0, "canary": 1, "rewrites": 0},
        "total_cost_usd": 0.0,
        "error_counts": {"error_runs": 0, "error_queries": 0},
        "iterations": iterations,
    }

    def checkpoint() -> None:
        blocks = [b for it in iterations for b in (it["train"], it["test"]) if isinstance(b, dict)]
        results["calls"]["made"] = len(all_calls)
        results["calls"]["rewrites"] = sum(c["kind"] == "rewrite" for c in all_calls)
        results["total_cost_usd"] = budget.spent
        results["error_counts"] = {
            "error_runs": sum(o["outcome"] == "error" for b in blocks for q in b["per_query"] for o in q["outcomes"]),
            "error_queries": sum(b["errors"] for b in blocks),
        }
        best = select_best(iterations, has_test)
        if best is None:
            results["best_description"], results["best_selection"] = None, None
        else:
            results["best_description"] = best["description"]
            results["best_selection"] = {
                "method": SELECTION_METHOD if has_test else TRAIN_ONLY_METHOD, "iteration": best["iteration"],
                "train_score": _score(best["train"]),
                "test_score": _score(best["test"]) if has_test else None,
            }
        _write_results(results_dir, results)

    try:
        cc.sweep_stale()
        try:
            canary = run_eval.preflight_canary(model, extra_args=extra_args, timeout=timeout, budget=budget)
        except run_eval.CanaryFailed as e:
            # A missing CLI or a spent budget means nothing was launched.
            launched = not isinstance(e.__cause__, (cc.ClaudeNotFound, cc.BudgetExceeded))
            results["exit_reason"] = "canary failed"
            results["canary_error"] = str(e)
            results["calls"]["made"] = results["calls"]["canary"] = int(launched)
            results["total_cost_usd"] = budget.spent
            _write_results(results_dir, results)
            raise
        results["cli_version"] = canary.version or results["cli_version"]
        all_calls.append(canary.call)
        remaining = plan["worst_case"] - 1
        per_call = canary.cost_usd
        log(f"Canary passed (CLI {canary.version}, ${per_call:.4f}). Calibrated estimate for the "
            f"remaining calls: about ${per_call * (plan['expected_min'] - 1):.2f}-"
            f"${per_call * (plan['expected_max'] - 1):.2f} expected, at most ${per_call * remaining:.2f} "
            "(notional on subscription auth).")
        log(f"Split: {len(train_set)} train, {len(test_set)} test (holdout={holdout})")

        current = original
        best_train = -1
        history: list[dict] = []
        pending_calls: list[dict] = [canary.call]
        exit_reason = f"max_iterations ({max_iterations})"

        for n in range(1, max_iterations + 1):
            log(f"\n=== Iteration {n}/{max_iterations}: {current}")
            it: dict = {"iteration": n, "description": current, "calls": pending_calls}
            queries = (train_set + test_set) if n == 1 else train_set
            ev = run_eval.evaluate_description(
                queries, skill_name=name, skill_md=skill_md, description=current, model=model,
                skill_dir=skill_dir, runs_per_query=runs_per_query, fixed_runs=fixed_runs,
                num_workers=num_workers, timeout=timeout, extra_args=extra_args, budget=budget)
            it["calls"].extend(ev.calls)
            all_calls.extend(ev.calls)
            train_pq = [q for q in ev.per_query if q["id"] in train_ids]
            it["train"] = _block(train_pq)
            it["test"] = _block([q for q in ev.per_query if q["id"] not in train_ids]) if (n == 1 and has_test) \
                else (None if not has_test else SKIPPED)
            iterations.append(it)
            incomplete = ev.budget_stopped

            if n > 1 and has_test and not incomplete and it["train"]["passed"] >= best_train:
                te = run_eval.evaluate_description(
                    test_set, skill_name=name, skill_md=skill_md, description=current, model=model,
                    skill_dir=skill_dir, runs_per_query=runs_per_query, fixed_runs=fixed_runs,
                    num_workers=num_workers, timeout=timeout, extra_args=extra_args, budget=budget)
                it["calls"].extend(te.calls)
                all_calls.extend(te.calls)
                it["test"] = _block(te.per_query)
                incomplete = te.budget_stopped
            elif n > 1 and has_test and incomplete:
                it["test"] = "not run (max_total_usd reached)"
            if incomplete:
                it["incomplete"] = True

            tr = it["train"]
            log(f"Train {_score(tr)} ({tr['errors']} error)  "
                f"Test {_score(it['test']) if isinstance(it['test'], dict) else it['test']}")
            checkpoint()

            if incomplete:
                exit_reason = "max_total_usd reached"
                break
            best_train = max(best_train, tr["passed"])
            history.append({"description": current, "train_passed": tr["passed"],
                            "train_total": tr["total"], "per_query": train_pq})
            if tr["passed"] == tr["total"]:
                exit_reason = "all train queries passed"
                break
            if n == max_iterations:
                break
            if tr["failed"] == 0:
                exit_reason = f"no train failures to learn from ({tr['errors']} queries errored)"
                break

            # Rewrite from train failures only. Check the listing cut first: text past
            # the cap is invisible, so apparent under-triggering may be truncation.
            if any(q["should_trigger"] and q["result"] == "fail" for q in train_pq):
                listed = len(current) + when_chars
                if listed > validation.MAX_LISTING_LENGTH:
                    warning = (
                        f"description + when_to_use is {listed} characters, over the "
                        f"{validation.MAX_LISTING_LENGTH}-character listing cut. Text past the cut is not "
                        "shown to Claude, so apparent under-triggering may come from truncation rather "
                        "than wording. Shorten the description or when_to_use.")
                    if warning not in results["listing_warnings"]:
                        results["listing_warnings"].append(warning)
                    log("WARNING: " + warning)
            try:
                rw = imp.improve_description(
                    name, skill_md, current, train_pq, history[:-1], model,
                    extra_args=extra_args, timeout=max(timeout, 180), budget=budget,
                    log_dir=log_dir, iteration=n + 1)
            except imp.RewriteFailed as e:
                all_calls.extend(e.calls)
                it["calls"].extend(e.calls)
                exit_reason = "max_total_usd reached" if e.budget_stopped else f"rewrite failed: {e}"
                log(f"Rewrite stopped the loop: {e}")
                checkpoint()
                break
            all_calls.extend(rw.calls)
            pending_calls = list(rw.calls)
            current = rw.description
            log(f"Proposed: {current}")
        results["exit_reason"] = exit_reason
    except BaseException as e:
        if results["exit_reason"] == "in progress":
            results["exit_reason"] = f"crashed: {e!r}"
        if not isinstance(e, run_eval.CanaryFailed):
            checkpoint()
        raise
    checkpoint()
    return results


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--eval-set", "eval_set_path", required=True, type=click.Path(exists=True, dir_okay=False),
              help="Trigger eval set JSON.")
@click.option("--skill-path", required=True, type=click.Path(exists=True, file_okay=False),
              help="Skill directory containing SKILL.md.")
@click.option("--model", required=True, help="Model for claude -p (use the session model).")
@click.option("--max-iterations", default=3, show_default=True, type=int, help="Maximum iterations.")
@click.option("--runs-per-query", default=3, show_default=True, type=int,
              help="Maximum runs per query (exactly this many with --fixed-runs).")
@click.option("--fixed-runs", is_flag=True, help="Always use --runs-per-query runs, not adaptive.")
@click.option("--holdout", default=0.4, show_default=True, type=float,
              help="Fraction of queries held out for testing (0 disables the test set).")
@click.option("--num-workers", default=5, show_default=True, type=int, help="Parallel queries.")
@click.option("--timeout", default=60, show_default=True, type=int, help="Seconds per trial call.")
@click.option("--verbose", is_flag=True, help="Print progress to stderr.")
@click.option("--results-dir", default=None, type=click.Path(file_okay=False),
              help="Write results.json, report.html and logs/ here "
                   "(default: ./<skill-name>-workspace/trigger-opt/<timestamp>/).")
@click.option("--max-total-usd", default=None, type=float,
              help="Stop launching calls once cumulative total_cost_usd crosses this.")
@click.option("--yes", is_flag=True, help="Confirm the plan and spend. Without it, print the plan and exit.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.argument("passthrough", nargs=-1, type=click.UNPROCESSED)
def main(eval_set_path, skill_path, model, max_iterations, runs_per_query, fixed_runs, holdout,
         num_workers, timeout, verbose, results_dir, max_total_usd, yes, as_json, passthrough):
    """Optimize a skill's trigger description. Extra `claude` flags go after `--`."""
    skill_dir = Path(skill_path).resolve()
    md_path = skill_dir / "SKILL.md"
    if not md_path.is_file():
        raise click.ClickException(f"No SKILL.md found at {skill_dir}")
    skill_md = md_path.read_text(encoding="utf-8")
    try:
        fm, _ = run_eval.check_runnable(skill_md)  # refusal comes before any CLI call
        bad_name = validation.check_name(fm["name"])
        if bad_name:  # the name builds the results path, so it must be safe first
            raise ValueError(bad_name)
        if runs_per_query < run_eval.MIN_RUNS:
            raise ValueError(f"--runs-per-query must be at least {run_eval.MIN_RUNS}")
        if max_iterations < 1:
            raise ValueError("--max-iterations must be at least 1")
        if not 0 <= holdout < 1:
            raise ValueError("--holdout must be at least 0 and below 1")
        eval_set = run_eval.load_eval_set(eval_set_path)
        cc.validate_passthrough(passthrough)
        budget = cc.Budget(max_total_usd)
        run_eval.with_description(skill_md, fm["description"])
        train_set, test_set = split_eval_set(eval_set, holdout)
        if not train_set:
            raise ValueError("the eval set is too small to leave any train queries")
    except (ValueError, json.JSONDecodeError) as e:
        raise click.ClickException(str(e))

    name = fm["name"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(results_dir).resolve() if results_dir \
        else (Path.cwd() / f"{name}-workspace" / "trigger-opt" / stamp).resolve()
    conflict = validation.skill_tree_conflict(out_dir)
    if out_dir == skill_dir or skill_dir in out_dir.parents or conflict:
        raise click.ClickException(
            f"The results directory {out_dir} is inside the skill directory ({conflict or skill_dir}); "
            "results never go inside a skill or a skills directory. Run from another directory "
            "or pass --results-dir.")
    clash = _add_dir_collision(passthrough, name)
    if clash:
        raise click.ClickException(
            f"--add-dir {clash} already holds a skill named {name!r}; trials could credit it "
            "instead of the candidate. Remove it from --add-dir.")

    plan = plan_loop(len(eval_set), len(train_set), runs_per_query, fixed_runs, max_iterations)
    if not yes:
        if as_json:
            click.echo(json.dumps({"plan": plan, "queries": {"total": len(eval_set), "train": len(train_set),
                                                             "test": len(test_set)},
                                   "calls_made": 0, "confirmed": False}, indent=2))
        else:
            click.echo("\n".join(plan_lines(plan, len(eval_set), len(train_set), len(test_set),
                                            runs_per_query, fixed_runs, max_iterations, model, passthrough)))
            click.echo("No calls were made. Re-run with --yes to start.")
        return

    if not results_dir:
        out_dir = _claim_default_dir(out_dir)
    try:
        results = run_loop(
            eval_set=eval_set, skill_dir=skill_dir, skill_md=skill_md, frontmatter=fm, model=model,
            max_iterations=max_iterations, runs_per_query=runs_per_query, fixed_runs=fixed_runs,
            holdout=holdout, num_workers=num_workers, timeout=timeout, extra_args=passthrough,
            budget=budget, results_dir=out_dir, verbose=verbose)
    except run_eval.CanaryFailed as e:
        click.echo(str(e), err=True)
        click.echo(f"Results: {out_dir}", err=True)
        sys.exit(3)

    if as_json:
        click.echo(json.dumps(results, indent=2))
        return
    click.echo(f"Exit: {results['exit_reason']}")
    sel = results["best_selection"]
    if sel:
        click.echo(f"Best: iteration {sel['iteration']} (train {sel['train_score']}, test {sel['test_score']})")
        click.echo(f"Best description (not applied):\n{results['best_description']}")
    else:
        click.echo("No candidate was fully tested; no best description.")
    for w in results["listing_warnings"]:
        click.echo(f"WARNING: {w}")
    ec = results["error_counts"]
    if ec["error_runs"]:
        click.echo(f"WARNING: {ec['error_runs']} error runs ({ec['error_queries']} queries reported error); "
                   "errors are not non-triggers.", err=True)
    click.echo(f"Spent ${results['total_cost_usd']:.4f} over {results['calls']['made']} calls.")
    click.echo(f"Results: {out_dir}")


if __name__ == "__main__":
    main()

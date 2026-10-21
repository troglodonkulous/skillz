#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#     "click",
#     "pyyaml",
# ]
# ///
"""Aggregate one iteration's runs into benchmark.json and benchmark.md.

Implements the workspace contract in references/schemas.md (spec section 3.5):

    <iteration>/
    +-- eval-<N>-<slug>/
    |   +-- eval_metadata.json
    |   +-- with_skill/run-1/{grading.json, timing.json, run.json, ...}
    |   +-- without_skill/run-1/{...}
    +-- benchmark.json
    +-- benchmark.md

Every discovered run-N/ directory appears in benchmark.json with a status
(succeeded, failed, contaminated). Only succeeded runs enter the statistics
and the delta. The delta is with_skill minus without_skill, by declared role.

Usage:
    uv run aggregate_benchmark.py <iteration-dir> --skill-name <name> [--json]
"""

import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import click

WITH_SKILL = "with_skill"
WITHOUT_SKILL = "without_skill"
ROLES = (WITH_SKILL, WITHOUT_SKILL)  # role order: the viewer colours by first appearance
# Token counts of these provenances are comparable with each other only within their own kind.
REAL_TOKEN_PROVENANCES = ("cli_result", "subagent_notification")
DEFAULT_BASELINE = ("no_skill", "no skill")


class AggregateError(Exception):
    """Unusable workspace or inconsistent eval metadata."""


def calculate_stats(values: list[float]) -> dict:
    """Calculate mean, stddev, min, max for a list of values."""
    if not values:
        return {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0}

    n = len(values)
    mean = sum(values) / n

    if n > 1:
        variance = sum((x - mean) ** 2 for x in values) / (n - 1)
        stddev = math.sqrt(variance)
    else:
        stddev = 0.0

    return {
        "mean": round(mean, 4),
        "stddev": round(stddev, 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


# --------------------------------------------------------------------------
# Reading files
# --------------------------------------------------------------------------

def _read_json(path: Path):
    """Return (data, error). error is 'missing' or 'not valid JSON'."""
    if not path.is_file():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None, "not valid JSON"


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _numbered_dirs(parent: Path, prefix: str) -> list[tuple[int, Path]]:
    """Subdirectories named `<prefix>-<digits>...`, sorted by that number."""
    pattern = re.compile(rf"^{prefix}-(\d+)")
    found = []
    for child in parent.iterdir():
        match = pattern.match(child.name)
        if child.is_dir() and match:
            found.append((int(match.group(1)), child))
    return sorted(found, key=lambda item: (item[0], item[1].name))


def find_eval_metadata(run_dir: Path, eval_dir: Path) -> dict | None:
    """Search upward from the run directory to the eval directory."""
    current = run_dir
    while True:
        data, _ = _read_json(current / "eval_metadata.json")
        if isinstance(data, dict):
            return data
        if current == eval_dir or current == current.parent:
            return None
        current = current.parent


# --------------------------------------------------------------------------
# Per-run classification
# --------------------------------------------------------------------------

def _run_json_shape_error(run_json: dict) -> str | None:
    """run_tasks always writes these fields. A record missing one, or holding
    the wrong type, cannot show the run was clean, so it is not trusted."""
    exit_code = run_json.get("exit_code", False)
    if exit_code is not None and not (isinstance(exit_code, int) and not isinstance(exit_code, bool)):
        return "exit_code"
    if run_json.get("is_error", 0) not in (True, False, None):
        return "is_error"
    if not isinstance(run_json.get("permission_denials"), list):
        return "permission_denials"
    if not _is_count(run_json.get("permission_denied_events")):
        return "permission_denied_events"
    return None


def run_error_reason(run_json: dict) -> str | None:
    """Return a reason when run.json records a run error, else None."""
    if field := _run_json_shape_error(run_json):
        return f"run.json: {field} missing or invalid"
    exit_code = run_json.get("exit_code")
    if exit_code not in (None, 0):
        return f"run.json: exit_code {exit_code}"
    if run_json.get("is_error") is True:
        return "run.json: is_error true"
    denials = run_json.get("permission_denials")
    if denials:
        names = sorted({d.get("tool_name", "?") for d in denials if isinstance(d, dict)}) or ["?"]
        return f"permission denied: {', '.join(names)}"
    events = run_json.get("permission_denied_events")
    if _is_number(events) and events > 0:
        return f"permission denied: {int(events)} event(s)"
    # The runner's own verdict covers what the fields above miss: a timeout,
    # no result event, an unverifiable baseline, a runner failure.
    if run_json.get("status") == "error":
        return f"run.json: {run_json.get('error') or 'status error'}"
    return None


def candidate_in_skills(skills, skill_name: str) -> bool:
    """True when `skills` lists the candidate exactly or as `<prefix>:<name>`."""
    if not isinstance(skills, list) or not skill_name:
        return False
    return any(
        isinstance(s, str) and (s == skill_name or s.endswith(":" + skill_name))
        for s in skills
    )


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_summary(summary) -> bool:
    """Counts are non-negative and add up; pass_rate is in [0, 1] and matches passed/total."""
    if not isinstance(summary, dict):
        return False
    passed, failed, total = (summary.get(k) for k in ("passed", "failed", "total"))
    rate = summary.get("pass_rate")
    if not (_is_count(passed) and _is_count(failed) and _is_count(total) and _is_number(rate)):
        return False
    if passed + failed != total or not 0 <= rate <= 1:
        return False
    expected = passed / total if total else 0.0
    return abs(rate - expected) <= 0.005 + 1e-9  # graders round pass_rate to 2 places


def _grading_result(grading: dict, timing: dict | None) -> dict:
    summary = grading["summary"]
    time_seconds = None
    if isinstance(timing, dict) and _is_number(timing.get("total_duration_seconds")):
        time_seconds = timing["total_duration_seconds"]
    else:
        grading_timing = grading.get("timing")
        if isinstance(grading_timing, dict) and _is_number(grading_timing.get("total_duration_seconds")):
            time_seconds = grading_timing["total_duration_seconds"]

    tokens = None
    provenance = "missing"
    if isinstance(timing, dict):
        raw = timing.get("total_tokens")
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            tokens = raw
        # wall_clock never measures tokens. A real provenance with no token
        # count, or an unknown provenance, is a missing measurement.
        recorded = timing.get("provenance")
        if recorded == "wall_clock":
            tokens, provenance = None, "wall_clock"
        elif recorded in REAL_TOKEN_PROVENANCES and tokens is not None:
            provenance = recorded
        else:
            tokens = None

    metrics = grading.get("execution_metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    tool_calls = metrics.get("total_tool_calls")
    errors = metrics.get("errors_encountered")
    return {
        "pass_rate": summary.get("pass_rate", 0.0),
        "passed": summary.get("passed", 0),
        "failed": summary.get("failed", 0),
        "total": summary.get("total", 0),
        "time_seconds": time_seconds,
        "tokens": tokens,
        "tokens_provenance": provenance,
        "tool_calls": tool_calls if _is_number(tool_calls) else None,
        "errors": errors if _is_number(errors) else None,
    }


def _flatten_notes(grading: dict) -> list[str]:
    summary = grading.get("user_notes_summary")
    if not isinstance(summary, dict):
        return []
    notes = []
    for key in ("uncertainties", "needs_review", "workarounds"):
        value = summary.get(key)
        if isinstance(value, list):
            notes.extend(str(item) for item in value)
    return notes


def load_run(run_dir: Path, run_number: int, configuration: str,
             eval_id: int, eval_name: str | None, skill_name: str) -> tuple[dict, dict | None]:
    """Classify one run directory. Returns (benchmark runs[] item, run.json or None)."""
    grading, grading_error = _read_json(run_dir / "grading.json")
    # Only an absent run.json is the subagent fallback; a broken one is a failed run.
    run_json, run_error = _read_json(run_dir / "run.json")
    if run_error == "missing":
        run_json, run_error = None, None
    elif run_error is None and not isinstance(run_json, dict):
        run_json, run_error = None, "is not a JSON object"
    elif run_error:
        run_json = None

    readable = (grading_error is None and isinstance(grading, dict)
                and isinstance(grading.get("expectations"), list) and _valid_summary(grading.get("summary")))
    status, reason = "succeeded", None
    if grading_error:
        status, reason = "failed", f"grading.json {'missing' if grading_error == 'missing' else 'is not valid JSON'}"
    elif not isinstance(grading, dict) or not isinstance(grading.get("expectations"), list):
        status, reason = "failed", "grading.json has no expectations array"
    elif not _valid_summary(grading.get("summary")):
        status, reason = "failed", "grading.json has no valid summary"
    elif run_error:
        status, reason = "failed", f"run.json {'is ' + run_error if run_error == 'not valid JSON' else run_error}"
    elif run_json is not None and (error := run_error_reason(run_json)):
        status, reason = "failed", error

    if status == "succeeded" and configuration == WITHOUT_SKILL and run_json is not None:
        # run_tasks records its own verdict, which knows about same-name snapshot
        # baselines; the name check is the fallback for records without one.
        recorded = run_json.get("contaminated")
        if isinstance(recorded, bool):
            if recorded:
                status, reason = "contaminated", "run.json records the baseline as contaminated"
        else:
            init = run_json.get("init")
            skills = init.get("skills") if isinstance(init, dict) else None
            if candidate_in_skills(skills, skill_name):
                status, reason = "contaminated", f'init.skills contains candidate "{skill_name}"'

    entry = {
        "eval_id": eval_id,
        "eval_name": eval_name,
        "configuration": configuration,
        "run_number": run_number,
        "status": status,
        "status_reason": reason,
        "result": None,
        "expectations": [],
        "notes": [],
    }
    if readable:
        timing, _ = _read_json(run_dir / "timing.json")
        entry["result"] = _grading_result(grading, timing)
        entry["expectations"] = grading["expectations"]
        entry["notes"] = _flatten_notes(grading)
    return entry, run_json


# --------------------------------------------------------------------------
# Discovery and aggregation
# --------------------------------------------------------------------------

def discover(iteration_dir: Path, skill_name: str) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Walk eval-*/{with_skill,without_skill}/run-*.

    Returns (runs, run_counts, run_jsons, eval_metadatas)."""
    eval_dirs = _numbered_dirs(iteration_dir, "eval")
    if not eval_dirs:
        raise AggregateError(f"no eval-<N>-<slug>/ directories in {iteration_dir}")

    runs, run_counts, run_jsons, metadatas = [], [], [], []
    for dir_number, eval_dir in eval_dirs:
        metadata, _ = _read_json(eval_dir / "eval_metadata.json")
        metadata = metadata if isinstance(metadata, dict) else {}
        eval_id = metadata.get("eval_id")
        if not isinstance(eval_id, int) or isinstance(eval_id, bool):
            eval_id = dir_number
        eval_name = metadata.get("eval_name") if isinstance(metadata.get("eval_name"), str) else None
        if metadata:
            metadatas.append(metadata)

        for configuration in ROLES:
            config_dir = eval_dir / configuration
            if not config_dir.is_dir():
                continue
            counts = {"eval_id": eval_id, "eval_name": eval_name, "configuration": configuration,
                      "discovered": 0, "succeeded": 0, "failed": 0, "contaminated": 0}
            for run_number, run_dir in _numbered_dirs(config_dir, "run"):
                run_meta = find_eval_metadata(run_dir, eval_dir) or {}
                name = run_meta.get("eval_name") if isinstance(run_meta.get("eval_name"), str) else eval_name
                entry, run_json = load_run(run_dir, run_number, configuration, eval_id, name, skill_name)
                runs.append(entry)
                counts["discovered"] += 1
                counts[entry["status"]] += 1
                if run_json is not None:
                    run_jsons.append(run_json)
            run_counts.append(counts)

    if not runs:
        raise AggregateError(f"no run-<N>/ directories under the eval directories in {iteration_dir}")
    return runs, run_counts, run_jsons, metadatas


def _stats_or_none(values: list) -> dict | None:
    values = [v for v in values if v is not None]
    return calculate_stats(values) if values else None


def mixed_token_provenance(runs: list[dict]) -> bool:
    """True when succeeded runs carry more than one real token provenance."""
    kinds = {r["result"]["tokens_provenance"] for r in runs if r["status"] == "succeeded"}
    return len(kinds & set(REAL_TOKEN_PROVENANCES)) > 1


def aggregate_runs(runs: list[dict]) -> dict:
    """run_summary over succeeded runs, plus the role-based delta.

    Token stats and the token delta are null when token provenances are mixed."""
    summary = {}
    mixed = mixed_token_provenance(runs)
    for configuration in ROLES:
        good = [r["result"] for r in runs
                if r["configuration"] == configuration and r["status"] == "succeeded"]
        summary[configuration] = {
            metric: None if (mixed and key == "tokens") else _stats_or_none([g[key] for g in good])
            for metric, key in (("pass_rate", "pass_rate"), ("time_seconds", "time_seconds"),
                                ("tokens", "tokens"))
        }

    def delta(metric: str, fmt: str):
        a, b = summary[WITH_SKILL][metric], summary[WITHOUT_SKILL][metric]
        if a is None or b is None:
            return None
        text = fmt % (a["mean"] - b["mean"])
        # A delta that rounds to zero carries no sign: "-0.00" becomes "+0.00".
        return "+" + text[1:] if text[0] == "-" and not text.strip("-+0.") else text

    summary["delta"] = {
        "pass_rate": delta("pass_rate", "%+.2f"),
        "time_seconds": delta("time_seconds", "%+.1f"),
        "tokens": delta("tokens", "%+.0f"),
    }
    return summary


def generate_benchmark(iteration_dir: Path, skill_name: str, skill_path: str = "") -> dict:
    runs, run_counts, run_jsons, metadatas = discover(iteration_dir, skill_name)
    runs.sort(key=lambda r: (r["eval_id"], ROLES.index(r["configuration"]), r["run_number"]))
    run_counts.sort(key=lambda c: (c["eval_id"], ROLES.index(c["configuration"])))

    # Baseline kind/label come from eval_metadata.json and must agree across evals.
    baselines = {(m.get("baseline_kind", DEFAULT_BASELINE[0]), m.get("baseline_label", DEFAULT_BASELINE[1]))
                 for m in metadatas}
    if len(baselines) > 1:
        raise AggregateError(f"evals disagree on baseline_kind/baseline_label: {sorted(baselines)}")
    baseline_kind, baseline_label = next(iter(baselines)) if baselines else DEFAULT_BASELINE

    notes = []

    # executor_model: most common init.model; say so when runs disagree.
    models = Counter(r["init"]["model"] for r in run_jsons
                     if isinstance(r.get("init"), dict) and isinstance(r["init"].get("model"), str))
    executor_model = models.most_common(1)[0][0] if models else None
    if len(models) > 1:
        notes.append("executor_model differs across runs (" +
                     ", ".join(f"{m} x{n}" for m, n in models.most_common()) +
                     f"); recorded the most common, {executor_model}")

    # Token provenance across succeeded runs.
    provenances = sorted({r["result"]["tokens_provenance"] for r in runs
                          if r["status"] == "succeeded" and r["result"]["tokens_provenance"] != "missing"})
    if any(r["status"] == "succeeded" and r["result"]["tokens_provenance"] == "missing" for r in runs):
        provenances.append("missing")
    if mixed_token_provenance(runs):
        notes.append("token counts mix provenances (" + ", ".join(provenances) +
                     "); token statistics and the token delta are omitted")

    for status in ("failed", "contaminated"):
        count = sum(1 for r in runs if r["status"] == status)
        if count:
            notes.append(f"{count} {status} run(s) excluded from the statistics and the delta")

    return {
        "metadata": {
            "skill_name": skill_name,
            "skill_path": skill_path,
            "executor_model": executor_model,
            "analyzer_model": None,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "evals_run": sorted({r["eval_id"] for r in runs}),
            "baseline_kind": baseline_kind,
            "baseline_label": baseline_label,
            "token_provenance": provenances,
            "run_counts": run_counts,
        },
        "runs": runs,
        "run_summary": aggregate_runs(runs),
        "notes": notes,
    }


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------

def _pm(stats: dict | None, scale: float, digits: int, unit: str) -> str:
    if stats is None:
        return "n/a"
    return f"{stats['mean'] * scale:.{digits}f}{unit} ± {stats['stddev'] * scale:.{digits}f}{unit}"


def generate_markdown(benchmark: dict) -> str:
    meta = benchmark["metadata"]
    summary = benchmark["run_summary"]
    delta = summary["delta"]
    baseline = meta["baseline_label"]

    lines = [
        f"# Skill Benchmark: {meta['skill_name']}",
        "",
        f"**Model**: {meta['executor_model'] or 'unknown'}",
        f"**Date**: {meta['timestamp']}",
        f"**Evals**: {', '.join(map(str, meta['evals_run']))}",
        "",
        "## Run counts",
        "",
        "| Eval | Configuration | Discovered | Succeeded | Failed | Contaminated |",
        "|------|---------------|------------|-----------|--------|--------------|",
    ]
    for c in meta["run_counts"]:
        label = f"{c['eval_id']}" + (f" ({c['eval_name']})" if c["eval_name"] else "")
        lines.append(f"| {label} | {c['configuration']} | {c['discovered']} | {c['succeeded']} "
                     f"| {c['failed']} | {c['contaminated']} |")

    ws, wo = summary[WITH_SKILL], summary[WITHOUT_SKILL]
    na = lambda v: "n/a" if v is None else v  # noqa: E731
    lines += [
        "",
        "## Summary",
        "",
        f"| Metric | with_skill | {baseline} | Delta |",
        "|--------|------------|------------|-------|",
        f"| Pass Rate | {_pm(ws['pass_rate'], 100, 0, '%')} | {_pm(wo['pass_rate'], 100, 0, '%')} | {na(delta['pass_rate'])} |",
        f"| Time | {_pm(ws['time_seconds'], 1, 1, 's')} | {_pm(wo['time_seconds'], 1, 1, 's')} | {na(delta['time_seconds'])} |",
        f"| Tokens | {_pm(ws['tokens'], 1, 0, '')} | {_pm(wo['tokens'], 1, 0, '')} | {na(delta['tokens'])} |",
        "",
        f"**Token provenance**: {', '.join(meta['token_provenance']) or 'n/a'}",
    ]

    excluded = [r for r in benchmark["runs"] if r["status"] != "succeeded"]
    if excluded:
        lines += ["", "## Failed and contaminated runs", ""]
        for r in excluded:
            lines.append(f"- eval {r['eval_id']} {r['configuration']} run-{r['run_number']}: "
                         f"{r['status']} ({r['status_reason']})")

    if benchmark["notes"]:
        lines += ["", "## Notes", ""]
        lines += [f"- {note}" for note in benchmark["notes"]]

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("iteration_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--skill-name", required=True,
              help="Name of the candidate skill. Used to detect contaminated baseline runs.")
@click.option("--skill-path", default="", help="Absolute path of the candidate skill.")
@click.option("--json", "as_json", is_flag=True,
              help="Print benchmark.json to stdout instead of a summary.")
def main(iteration_dir: Path, skill_name: str, skill_path: str, as_json: bool):
    """Aggregate the runs in ITERATION_DIR into benchmark.json and benchmark.md.

    Writes both files into ITERATION_DIR. The delta is with_skill minus
    without_skill. Failed and contaminated runs are listed and excluded.
    """
    try:
        benchmark = generate_benchmark(iteration_dir, skill_name, skill_path)
    except AggregateError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    (iteration_dir / "benchmark.json").write_text(json.dumps(benchmark, indent=2) + "\n", encoding="utf-8")
    (iteration_dir / "benchmark.md").write_text(generate_markdown(benchmark), encoding="utf-8")

    if as_json:
        click.echo(json.dumps(benchmark, indent=2))
        return
    click.echo(f"Generated: {iteration_dir / 'benchmark.json'}")
    click.echo(f"Generated: {iteration_dir / 'benchmark.md'}")
    summary = benchmark["run_summary"]
    for configuration in ROLES:
        pr = summary[configuration]["pass_rate"]
        click.echo(f"  {configuration}: " + ("n/a" if pr is None else f"{pr['mean'] * 100:.1f}% pass rate"))
    click.echo(f"  delta: {summary['delta']['pass_rate'] or 'n/a'}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["click", "pyyaml"]
# ///
"""Rewrite a skill description from trigger-eval failures.

Library (run_loop.py imports it) and standalone CLI. The rewrite runs through
claude_cli's REWRITE kind: no tools, isolated temp cwd, `dontAsk`. It reads the
skill with a full YAML parse, so `when_to_use` is context for the rewrite and
the combined listing cap (spec 2.4) is part of the prompt and of the checks.

Library API (import by bare name after putting scripts/eval on sys.path)
------------------------------------------------------------------------
    improve_description(skill_name, skill_md, current_description, train_results,
                        history, model, *, extra_args=(), timeout=180, budget=None,
                        log_dir=None, iteration=None) -> RewriteResult
        train_results: per_query[] (schemas.md) of the TRAIN set only. Only
        queries with result "fail" are shown to the model; "error" queries
        carry no signal about the description and are left out.
        history: earlier attempts, each {"description", "train_passed",
        "train_total", "per_query"}. The caller builds it from train data only;
        this module never sees held-out test queries.
        Returns RewriteResult(description, calls, cost_usd, attempts). The
        description passes check_description(): non-empty, no < or >, at most
        1,024 chars, and at most 1,536 chars together with `when_to_use`.
        On an unusable answer the call is retried (max 3 attempts, each with
        the problem stated). Raises RewriteFailed (carries .calls, .cost_usd,
        .budget_stopped) when attempts run out, a call errors, or the
        --max-total-usd cap is reached.
    check_description(description, when_to_use) -> list[str]
        Problems that make a description unusable; empty when it is fine.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import claude_cli as cc  # noqa: E402
import run_eval  # noqa: E402
import validation  # noqa: E402

MAX_ATTEMPTS = 3
_TAG = re.compile(r"<new_description>(.*?)</new_description>", re.DOTALL)


class RewriteFailed(RuntimeError):
    """No usable description was produced; the message says why."""

    def __init__(self, message: str, calls: list[dict], cost_usd: float, budget_stopped: bool = False):
        super().__init__(message)
        self.calls = calls
        self.cost_usd = cost_usd
        self.budget_stopped = budget_stopped


@dataclass
class RewriteResult:
    description: str
    calls: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0
    attempts: int = 1


def check_description(description: str, when_to_use: str | None = None) -> list[str]:
    problems = []
    if not description.strip():
        return ["the description is empty"]
    if "<" in description or ">" in description:
        problems.append("the description contains '<' or '>', which are not allowed")
    if len(description) > validation.MAX_DESCRIPTION_LENGTH:
        problems.append(
            f"the description is {len(description)} characters; the limit is "
            f"{validation.MAX_DESCRIPTION_LENGTH}")
    combined = len(description) + len(when_to_use or "")
    if combined > validation.MAX_LISTING_LENGTH:
        problems.append(
            f"description + when_to_use is {combined} characters; the skill listing cuts "
            f"them at {validation.MAX_LISTING_LENGTH} combined")
    return problems


def _counts(q: dict) -> tuple[int, int]:
    outcomes = [o["outcome"] for o in q.get("outcomes", []) if o.get("outcome") != "error"]
    return sum(o == "triggered" for o in outcomes), len(outcomes)


def _failure_lines(per_query: list[dict]) -> str:
    missed, false_hits = [], []
    for q in per_query:
        if q.get("result") != "fail":
            continue
        t, n = _counts(q)
        line = f'  - "{q["query"]}" (triggered {t}/{n} times)\n'
        (missed if q["should_trigger"] else false_hits).append(line)
    out = ""
    if missed:
        out += "FAILED TO TRIGGER (should have triggered but didn't):\n" + "".join(missed) + "\n"
    if false_hits:
        out += "FALSE TRIGGERS (triggered but shouldn't have):\n" + "".join(false_hits) + "\n"
    return out


def build_prompt(skill_name: str, frontmatter: dict, body: str, current_description: str,
                 train_results: list[dict], history: list[dict]) -> str:
    passed = sum(q.get("result") == "pass" for q in train_results)
    unmeasured = sum(q.get("result") == "error" for q in train_results)
    scores = f"Train: {passed}/{len(train_results)}"
    if unmeasured:
        scores += f" ({unmeasured} queries could not be measured and are left out below)"
    when_to_use = frontmatter.get("when_to_use")
    when_block = (
        f"\nThe skill also has a `when_to_use` field. Claude Code appends it to the description in the "
        f"skill listing, so the two together are what Claude reads. Do not repeat it; write the "
        f"description to complement it:\n<when_to_use>\n{when_to_use}\n</when_to_use>\n"
        if isinstance(when_to_use, str) and when_to_use.strip() else ""
    )
    prompt = f"""You are optimizing a skill description for a Claude Code skill called "{skill_name}". A "skill" is sort of like a prompt, but with progressive disclosure -- there's a title and description that Claude sees when deciding whether to use the skill, and then if it does use the skill, it reads the .md file which has lots more details and potentially links to other resources in the skill folder like helper files and scripts and additional documentation or examples.

The description appears in Claude's skill listing. When a user sends a query, Claude decides whether to invoke the skill based solely on the skill's name and this description. Your goal is to write a description that triggers for relevant queries, and doesn't trigger for irrelevant ones.

Here's the current description:
<current_description>
{json.dumps(current_description, ensure_ascii=False)}
</current_description>
{when_block}
Current scores ({scores}):
<scores_summary>
"""
    prompt += _failure_lines(train_results) or "No failures to report.\n\n"
    if history:
        prompt += "PREVIOUS ATTEMPTS (do NOT repeat these -- try something structurally different):\n\n"
        for h in history:
            prompt += f'<attempt train={h["train_passed"]}/{h["train_total"]}>\n'
            prompt += f'Description: {json.dumps(h["description"], ensure_ascii=False)}\n'
            prompt += "Train results:\n"
            for q in h.get("per_query", []):
                if q.get("result") == "error":
                    continue
                t, n = _counts(q)
                prompt += f'  [{q["result"].upper()}] "{q["query"][:80]}" (triggered {t}/{n})\n'
            prompt += "</attempt>\n\n"

    prompt += f"""</scores_summary>

Skill content (for context on what the skill does):
<skill_content>
{body}
</skill_content>

Based on the failures, write a new and improved description that is more likely to trigger correctly. When I say "based on the failures", it's a bit of a tricky line to walk because we don't want to overfit to the specific cases you're seeing. So what I DON'T want you to do is produce an ever-expanding list of specific queries that this skill should or shouldn't trigger for. Instead, try to generalize from the failures to broader categories of user intent and situations where this skill would be useful or not useful. The reason for this is twofold:

1. Avoid overfitting
2. The list might get loooong and it's injected into ALL queries and there might be a lot of skills, so we don't want to blow too much space on any given description.

Concretely, your description should not be more than about 100-200 words, even if that comes at the cost of accuracy. Two hard limits apply: the description must be at most {validation.MAX_DESCRIPTION_LENGTH} characters, and the skill listing cuts `description` and `when_to_use` at {validation.MAX_LISTING_LENGTH} characters combined, so keep the two together under that. Do not use the characters < or > in the description. Stay comfortably under the limits.

Here are some tips that we've found to work well in writing these descriptions:
- The skill should be phrased in the imperative -- "Use this skill for" rather than "this skill does"
- The skill description should focus on the user's intent, what they are trying to achieve, vs. the implementation details of how the skill works.
- The description competes with other skills for Claude's attention -- make it distinctive and immediately recognizable.
- If you're getting lots of failures after repeated attempts, change things up. Try different sentence structures or wordings.

I'd encourage you to be creative and mix up the style in different iterations since you'll have multiple opportunities to try different approaches and we'll just grab the highest-scoring one at the end.

Please respond with only the new description text in <new_description> tags, nothing else."""
    return prompt


def _extract(text: str) -> str | None:
    m = _TAG.search(text)
    if not m:
        return None
    return " ".join(m.group(1).strip().strip('"').split())


def improve_description(
    skill_name: str,
    skill_md: str,
    current_description: str,
    train_results: list[dict],
    history: list[dict],
    model: str,
    *,
    extra_args=(),
    timeout: float = 180,
    budget: cc.Budget | None = None,
    log_dir: Path | str | None = None,
    iteration: int | None = None,
) -> RewriteResult:
    frontmatter, body, err = validation.parse_frontmatter(skill_md)
    if frontmatter is None:
        raise ValueError(err)
    when_to_use = frontmatter.get("when_to_use")
    when_to_use = when_to_use if isinstance(when_to_use, str) else None
    base_prompt = build_prompt(skill_name, frontmatter, body, current_description,
                               train_results, history)

    calls: list[dict] = []
    cost = 0.0
    transcript: list[dict] = []
    prompt = base_prompt
    description = ""
    problems: list[str] = []

    def fail(message: str, budget_stopped: bool = False) -> RewriteFailed:
        _write_log(log_dir, iteration, base_prompt, transcript, None)
        return RewriteFailed(message, calls, cost, budget_stopped)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        call = cc.build_argv(cc.REWRITE, prompt, model=model, extra_args=extra_args)
        try:
            with cc.temp_project() as project:
                out = cc.run_and_parse(call, project, timeout=timeout, budget=budget)
        except cc.BudgetExceeded as e:
            raise fail(f"max-total-usd reached before the rewrite: {e}", True) from e
        except cc.ClaudeNotFound as e:
            raise fail(str(e)) from e
        calls.append({"kind": "rewrite", "argv": list(call.argv), "extra_args": list(call.extra_args),
                      "exit_code": out.raw.returncode, "total_cost_usd": out.summary.total_cost_usd})
        cost += out.cost_usd
        if out.status == "error":
            transcript.append({"attempt": attempt, "error": out.error})
            tail = out.raw.stderr_tail.strip()
            raise fail(f"the rewrite call failed: {out.error}" + (f"\nstderr: {tail}" if tail else ""))
        text = (out.summary.result or {}).get("result") or ""
        parsed = _extract(text)
        problems = ["the answer had no <new_description> tags"] if parsed is None \
            else check_description(parsed, when_to_use)
        transcript.append({"attempt": attempt, "response": text, "parsed_description": parsed,
                           "problems": problems})
        if not problems:
            description = parsed
            break
        prompt = (
            f"{base_prompt}\n\n---\n\nA previous attempt was rejected: {'; '.join(problems)}.\n"
            + (f'It was:\n"{parsed}"\n\n' if parsed else "\n")
            + "Write a corrected description that fixes this. Respond with only the new "
              "description in <new_description> tags."
        )
    else:
        raise fail(f"no usable description after {MAX_ATTEMPTS} attempts: {'; '.join(problems)}")

    _write_log(log_dir, iteration, base_prompt, transcript, description)
    return RewriteResult(description, calls, cost, attempt)


def _write_log(log_dir, iteration, prompt, transcript, final) -> None:
    if not log_dir:
        return
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    name = f"improve_iter_{iteration if iteration is not None else 'unknown'}.json"
    (log_dir / name).write_text(json.dumps(
        {"iteration": iteration, "prompt": prompt, "attempts": transcript,
         "final_description": final}, indent=2), encoding="utf-8")


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--eval-results", required=True, type=click.Path(exists=True, dir_okay=False),
              help="JSON from `run_eval.py --json` (needs `description`, `results`).")
@click.option("--skill-path", required=True, type=click.Path(exists=True, file_okay=False),
              help="Skill directory containing SKILL.md.")
@click.option("--history", "history_path", default=None, type=click.Path(exists=True, dir_okay=False),
              help="JSON list of earlier attempts: {description, train_passed, train_total, per_query}.")
@click.option("--model", required=True, help="Model for the rewrite.")
@click.option("--timeout", default=180, show_default=True, type=int, help="Seconds per claude call.")
@click.option("--max-total-usd", default=None, type=float,
              help="Stop launching calls once cumulative total_cost_usd crosses this.")
@click.option("--verbose", is_flag=True, help="Print progress to stderr.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (default is the bare description).")
@click.argument("passthrough", nargs=-1, type=click.UNPROCESSED)
def main(eval_results, skill_path, history_path, model, timeout, max_total_usd, verbose, as_json,
         passthrough):
    """Rewrite a description from eval failures. Extra `claude` flags go after `--`."""
    md_path = Path(skill_path) / "SKILL.md"
    if not md_path.is_file():
        raise click.ClickException(f"No SKILL.md found at {skill_path}")
    skill_md = md_path.read_text(encoding="utf-8")
    try:
        data = json.loads(Path(eval_results).read_text(encoding="utf-8"))
        history = json.loads(Path(history_path).read_text(encoding="utf-8")) if history_path else []
        fm, _ = run_eval.check_runnable(skill_md)  # refusal comes before any CLI call
        cc.validate_passthrough(passthrough)
        budget = cc.Budget(max_total_usd)
        per_query = data["results"]
        current = data["description"]
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        raise click.ClickException(f"bad input: {e}")
    if verbose:
        click.echo(f"Current: {current}", err=True)
    try:
        res = improve_description(
            fm.get("name", Path(skill_path).name), skill_md, current, per_query, history, model,
            extra_args=passthrough, timeout=timeout, budget=budget)
    except RewriteFailed as e:
        raise click.ClickException(str(e))
    if verbose:
        click.echo(f"Improved: {res.description}", err=True)
    if as_json:
        passed = sum(q.get("result") == "pass" for q in per_query)
        click.echo(json.dumps({
            "description": res.description,
            "attempts": res.attempts,
            "total_cost_usd": res.cost_usd,
            "calls": res.calls,
            "history": history + [{"description": current, "train_passed": passed,
                                   "train_total": len(per_query), "per_query": per_query}],
        }, indent=2))
    else:
        click.echo(res.description)


if __name__ == "__main__":
    main()

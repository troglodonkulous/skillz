#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["click", "pyyaml"]
# ///
"""HTML report for a trigger-optimization results.json (schemas.md).

Self-contained: inline CSS, no web fonts, no scripts, so it opens offline.
Shows, per iteration, train and test scores, a skipped test with its reason,
error counts, runs used, and a per-query grid.

Library: generate_html(results: dict) -> str
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

import click

CSS = """
body{font-family:-apple-system,system-ui,Segoe UI,Helvetica,Arial,sans-serif;margin:0;padding:20px;background:#faf9f5;color:#141413;line-height:1.45}
h1{font-size:1.4rem;margin:0 0 4px}h2{font-size:1.05rem;margin:24px 0 8px}
.meta{color:#5d5c55;font-size:.9rem}.card{background:#fff;border:1px solid #e8e6dc;border-radius:6px;padding:12px 16px;margin:12px 0}
.warn{background:#fff4e5;border-color:#f0c58a}.bad{background:#fdecea;border-color:#e5a29b}
table{border-collapse:collapse;width:100%;background:#fff;font-size:.85rem}
th,td{border:1px solid #e8e6dc;padding:6px 8px;vertical-align:top;text-align:left}
th{background:#f1efe6}.grid td.c{text-align:center;white-space:nowrap}
.pass{background:#e7f4e4}.fail{background:#fdecea}.error{background:#fff4e5}.na{background:#f4f4f2;color:#8a897f}
.best{outline:2px solid #3b7a2e;outline-offset:-2px}.desc{max-width:34rem;white-space:pre-wrap}
.test{border-left:3px solid #4a6fa5}code{background:#f1efe6;padding:1px 4px;border-radius:3px}
.sm{font-size:.75rem;color:#5d5c55}
@media (prefers-color-scheme:dark){body{background:#1c1b19;color:#ece9df}.card,table{background:#262522}
th{background:#33312d}th,td,.card{border-color:#44413b}.meta,.sm{color:#a6a399}
.pass{background:#23401f}.fail{background:#4a2420}.error{background:#4a3a1c}.na{background:#2f2e2b}
.warn{background:#3b2f1a}.bad{background:#4a2420}code{background:#33312d}}
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _score(block) -> tuple[str, str]:
    """(text, css class) for a train/test block."""
    if block is None:
        return "no held-out set", "na"
    if isinstance(block, str):
        return block, "na"
    text = f"{block['passed']}/{block['total']}"
    if block.get("errors"):
        text += f" ({block['errors']} error)"
    ratio = block["passed"] / block["total"] if block["total"] else 0
    return text, "pass" if ratio >= 0.8 else "error" if ratio >= 0.5 else "fail"


def _runs(block) -> int:
    if not isinstance(block, dict):
        return 0
    return sum(q.get("runs_used", 0) for q in block.get("per_query", []))


def _cell(q: dict | None) -> str:
    if q is None:
        return '<td class="c na">&ndash;</td>'
    rate = q.get("trigger_rate")
    rate_text = "n/a" if rate is None else f"{rate:.2f}"
    mark = {"pass": "&#10003;", "fail": "&#10007;", "error": "!"}[q["result"]]
    return (f'<td class="c {q["result"]}" title="expected trigger={q["should_trigger"]}">'
            f'{mark}<br><span class="sm">{rate_text} &middot; {q["runs_used"]} runs</span></td>')


def generate_html(data: dict) -> str:
    name = data.get("skill_name", "")
    iterations = data.get("iterations", [])
    selection = data.get("best_selection") or {}
    best_iter = selection.get("iteration")
    settings = data.get("settings", {})
    calls = data.get("calls", {})
    errors = data.get("error_counts", {})
    listing = data.get("listing_settings", {})

    parts = [
        "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{_e(name)} trigger optimization</title><style>{CSS}</style></head><body>",
        f"<h1>{_e(name)}: trigger optimization</h1>",
        f'<div class="meta">Started {_e(data.get("started_at"))} &middot; model {_e(settings.get("model"))}'
        f' &middot; CLI {_e(data.get("cli_version"))} &middot; exit: {_e(data.get("exit_reason"))}</div>',
    ]

    for w in data.get("listing_warnings") or []:
        parts.append(f'<div class="card warn"><strong>Listing:</strong> {_e(w)}</div>')
    if errors.get("error_runs"):
        parts.append(
            f'<div class="card bad"><strong>{_e(errors["error_runs"])} error runs</strong> '
            f'({_e(errors.get("error_queries"))} queries reported error). Errors are infrastructure '
            "failures, not non-triggers, and are not counted as passes or fails.</div>")

    cost = data.get("total_cost_usd")
    parts.append(
        '<div class="card"><table><tbody>'
        f'<tr><th>Calls</th><td>{_e(calls.get("made"))} made of {_e(calls.get("planned_worst_case"))} '
        f'worst case ({_e(calls.get("canary"))} canary, {_e(calls.get("rewrites"))} rewrites)</td></tr>'
        f'<tr><th>Cost</th><td>{"n/a" if cost is None else f"${cost:.4f}"} (notional on subscription auth)</td></tr>'
        f'<tr><th>Settings</th><td>up to {_e(settings.get("runs_per_query"))} runs per query'
        f'{", fixed" if settings.get("fixed_runs") else ", adaptive"}; max {_e(settings.get("max_iterations"))} '
        f'iterations; holdout {_e(settings.get("holdout"))}; threshold {_e(settings.get("threshold"))}</td></tr>'
        f'<tr><th>Listing</th><td>description {_e(listing.get("description_chars"))} chars, when_to_use '
        f'{_e(listing.get("when_to_use_chars"))} chars; '
        f'SLASH_COMMAND_TOOL_CHAR_BUDGET={_e(listing.get("SLASH_COMMAND_TOOL_CHAR_BUDGET"))}, '
        f'skillListingBudgetFraction={_e(listing.get("skillListingBudgetFraction"))}, '
        f'skillListingMaxDescChars={_e(listing.get("skillListingMaxDescChars"))}, '
        f'skillOverrides={_e(listing.get("skillOverrides"))}<br>'
        f'<span class="sm">{_e(listing.get("trial_listing"))}</span></td></tr>'
        "</tbody></table></div>")

    parts.append("<h2>Descriptions</h2>")
    parts.append(f'<div class="card"><strong>Original</strong><div class="desc">'
                 f'{_e(data.get("original_description"))}</div></div>')
    if data.get("best_description") is not None:
        parts.append(
            f'<div class="card best"><strong>Best</strong> (iteration {_e(best_iter)}, train '
            f'{_e(selection.get("train_score"))}, test {_e(selection.get("test_score"))}). '
            f'<span class="sm">Reported, not applied: the skill file was not changed.</span>'
            f'<div class="desc">{_e(data["best_description"])}</div></div>')
    else:
        parts.append('<div class="card warn">No candidate was fully tested, so no best description was selected.</div>')

    parts.append("<h2>Iterations</h2><table><thead><tr><th>#</th><th>Train</th><th>Test</th>"
                 "<th>Runs used</th><th>Errors</th><th>Description</th></tr></thead><tbody>")
    for it in iterations:
        train_text, train_cls = _score(it.get("train"))
        test_text, test_cls = _score(it.get("test"))
        err_runs = sum(o["outcome"] == "error" for blk in (it.get("train"), it.get("test"))
                       if isinstance(blk, dict) for q in blk.get("per_query", []) for o in q["outcomes"])
        incomplete = ' <span class="sm">(incomplete: cost cap reached)</span>' if it.get("incomplete") else ""
        parts.append(
            f'<tr class="{"best" if it["iteration"] == best_iter else ""}"><td>{_e(it["iteration"])}</td>'
            f'<td class="{train_cls}">{_e(train_text)}</td><td class="{test_cls} test">{_e(test_text)}</td>'
            f'<td>{_runs(it.get("train")) + _runs(it.get("test"))}</td><td>{err_runs}</td>'
            f'<td class="desc">{_e(it["description"])}{incomplete}</td></tr>')
    parts.append("</tbody></table>")

    # Per-query grid: columns are the queries of iteration 1 (train, then test), keyed by id.
    if iterations:
        first = iterations[0]
        cols = [("train", q) for q in first["train"]["per_query"]]
        if isinstance(first.get("test"), dict):
            cols += [("test", q) for q in first["test"]["per_query"]]
        parts.append("<h2>Per query</h2><table class=\"grid\"><thead><tr><th>#</th>")
        for split, q in cols:
            parts.append(f'<th class="{"test" if split == "test" else ""}" title="{_e(q["query"])}">'
                         f'{_e(q["id"])}<br><span class="sm">{_e(split)}, expect '
                         f'{"trigger" if q["should_trigger"] else "no trigger"}</span></th>')
        parts.append("</tr></thead><tbody>")
        for it in iterations:
            parts.append(f'<tr><td>{_e(it["iteration"])}</td>')
            for split, q in cols:
                block = it.get(split)
                found = None
                if isinstance(block, dict):
                    found = next((r for r in block["per_query"] if r["id"] == q["id"]), None)
                parts.append(_cell(found))
            parts.append("</tr>")
        parts.append("</tbody></table>")
        parts.append("<h2>Queries</h2><table><thead><tr><th>id</th><th>split</th><th>expected</th>"
                     "<th>query</th></tr></thead><tbody>")
        for split, q in cols:
            parts.append(f'<tr><td>{_e(q["id"])}</td><td>{_e(split)}</td>'
                         f'<td>{"trigger" if q["should_trigger"] else "no trigger"}</td>'
                         f'<td>{_e(q["query"])}</td></tr>')
        parts.append("</tbody></table>")

    parts.append("</body></html>\n")
    return "".join(parts)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("results", type=click.Path(dir_okay=False, allow_dash=True))
@click.option("-o", "--output", default=None, type=click.Path(dir_okay=False),
              help="Write the HTML here (default: stdout).")
@click.option("--json", "as_json", is_flag=True,
              help="With -o, print {output, bytes} as JSON instead of a plain line.")
def main(results, output, as_json):
    """Build report.html from a trigger-optimization results.json (or - for stdin)."""
    text = sys.stdin.read() if results == "-" else Path(results).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
        page = generate_html(data)
    except (ValueError, KeyError, TypeError) as e:
        raise click.ClickException(f"cannot build a report from this input: {e!r}")
    if not output:
        click.echo(page, nl=False)
        return
    Path(output).write_text(page, encoding="utf-8")
    if as_json:
        click.echo(json.dumps({"output": str(output), "bytes": len(page.encode("utf-8"))}))
    else:
        click.echo(f"Wrote {output}")


if __name__ == "__main__":
    main()

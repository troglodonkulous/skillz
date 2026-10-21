# Trigger optimization

Tune a skill's `description` so Claude loads it when it should and not otherwise. Run it when the skill works but fires too rarely or too often. Requires the `claude` CLI and a model call per trial. Formats are in [schemas.md](schemas.md); this file does not repeat them.

## Contents

- [Preconditions](#preconditions)
- [1. Design the 20 queries](#1-design-the-20-queries)
- [2. User review](#2-user-review)
- [3. Check the listing before rewriting](#3-check-the-listing-before-rewriting)
- [4. Run the loop](#4-run-the-loop)
- [Cost guide](#cost-guide)
- [Results](#results)
- [5. Apply the best description](#5-apply-the-best-description)
- [Single-step tools](#single-step-tools)
- [CLI version and tests](#cli-version-and-tests)

## Preconditions

- The `claude` CLI is on `PATH`. There is no subagent fallback: the measurement needs real skill listing and invocation.
- Refuse a skill with `disable-model-invocation: true`. It never auto-triggers by design, so there is nothing to optimize. The scripts also refuse it, before any call. Tell the user why.
- Use the model that powers this session for `--model` (take the id from your system prompt), so the measurement matches what the user experiences.

## 1. Design the 20 queries

Write 20 queries: 8-10 that should trigger the skill and 8-10 that should not. Save as the eval set JSON (shape in [schemas.md](schemas.md#trigger-eval-set)), by default at `<skill-name>-workspace/trigger-opt/eval-set.json`.

Make them realistic: things a user would actually type, with concrete detail such as file paths, column names, a company, a bit of backstory. Mix lengths, include typos, abbreviations and casual speech. Weak: `"Format this data"`, `"Extract text from PDF"`. Strong:

```
ok so my boss sent me this xlsx (its in downloads, called something like 'Q4 sales final FINAL v2.xlsx') and wants a profit margin column. revenue is col C and costs col D i think
```

Should-trigger queries: vary phrasing (formal and casual), include cases where the user never names the skill or file type but clearly needs it, uncommon use cases, and cases where this skill competes with another and should win.

Should-not-trigger queries: the useful ones are near misses. They share keywords or concepts with the skill but need something different: adjacent domains, phrasing a keyword match would wrongly accept, or tasks where another tool is the right one. Negatives like "write a fibonacci function" for a PDF skill test nothing.

Make queries substantive. Claude consults a skill only for work it cannot do easily by itself, so one-step requests such as "read this file" do not trigger skills whatever the description says.

If the skill has `paths` in its frontmatter, it only loads when matching files are in play. Write those queries with matching file context (mention a path that matches the glob), and say so in the review.

## 2. User review

Bad queries produce a bad description, so the user reviews the set before anything runs.

With a browser: fill skillz's own template `${CLAUDE_SKILL_DIR}/assets/eval_review.html` (not a file of the skill being optimized) by replacing three placeholders: `__EVAL_DATA_PLACEHOLDER__` with the JSON array (no quotes around it, it is a JS assignment), `__SKILL_NAME_PLACEHOLDER__` with the skill name, `__SKILL_DESCRIPTION_PLACEHOLDER__` with the current description. Write the result outside any skills directory (for example into the workspace) and open it. The user edits queries, toggles `should_trigger`, adds or removes entries, and clicks Export Eval Set, which downloads `eval_set.json` (the newest copy, if there are several, in `~/Downloads`). Move it to the eval set path.

With no browser: present the set as a table in chat (number, query, should trigger yes or no) and apply the user's edits by hand.

## 3. Check the listing before rewriting

Claude Code's skill listing cuts `description` plus `when_to_use` at 1,536 characters combined. A skill that seems to under-trigger may simply be cut off. Before rewriting, compare the combined length with that cap, and check the user's listing settings: `SLASH_COMMAND_TOOL_CHAR_BUDGET`, `skillListingBudgetFraction`, `skillListingMaxDescChars`, and any `skillOverrides` entry for the skill. If the text is being cut, shorten it and move the key trigger phrases to the front. A rewrite loop will not fix truncation.

Trials run with `--setting-sources project`, so they use the default listing with about 20 skills. A trial may be kinder than a crowded real session; tell the user. `run_loop.py` records the effective settings in `results.json` `listing_settings`, and adds `listing_warnings[]` when the text exceeds the cut while should-trigger queries fail; if that field is non-empty, address it first.

## 4. Run the loop

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/eval/run_loop.py \
  --eval-set <eval-set.json> --skill-path <skill-path> \
  --model <session-model-id> --verbose \
  [--max-iterations 3] [--runs-per-query 3] [--fixed-runs] [--holdout 0.4] \
  [--num-workers 5] [--timeout 60] [--results-dir <dir>] [--max-total-usd <n>] \
  [--yes] [--json] [-- <claude -p args>]
```

Run it without `--yes` first. It prints the plan, the call budget (queries x runs x iterations, plus canary and rewrites), and makes no call. Show that to the user, and after they confirm, rerun with `--yes` in the background and report progress as iterations finish.

| Flag | Meaning |
|---|---|
| `--eval-set FILE`, `--skill-path DIRECTORY`, `--model TEXT` | Required. |
| `--max-iterations N` | Default 3. Gains usually flatten by the third. Use 5 only if the third was still improving. |
| `--runs-per-query N` | Maximum runs per query, default 3. By default runs are adaptive: 2, and a third only when the first two disagree. |
| `--fixed-runs` | Always exactly `--runs-per-query` runs. |
| `--holdout F` | Fraction held out as a test set, default 0.4. `0` disables the test set, so the best description is chosen by train score alone and may be overfit. Avoid it unless the user insists. |
| `--num-workers N` | Parallel queries, default 5. |
| `--timeout S` | Seconds per trial, default 60. |
| `--results-dir DIR` | Override where results are written. |
| `--max-total-usd N` | Stop launching calls once cumulative cost crosses `N`. |
| `--yes` | Confirm the plan and spend. |
| `--verbose`, `--json` | Progress to stderr; machine-readable output. |

Everything after `--` goes to `claude -p` verbatim, subject to the same rules as in [evaluation.md](evaluation.md#3-run-plan-first-then-spend), including that `--add-dir` takes absolute paths only. `run_loop.py` refuses a `--results-dir` inside any skill directory, `.claude/skills` tree, or plugin `skills/` directory.

What it does:

1. A canary runs first: a throwaway skill that must trigger. If it fails, the loop aborts and prints the CLI version, exit code and stderr (`exit_reason: "canary failed"`, with `canary_error`). Do not interpret a failed canary as a skill problem; fix the environment.
2. Iteration 1 scores the original description on the train and test queries.
3. Each later iteration rewrites the description from the train failures only, then scores it. The test set is re-run only if the candidate's train score ties or beats the best so far; otherwise the iteration's `test` is `"skipped (train regressed)"` and it cannot be selected.
4. It stops early when every train query passes.
5. It reports `best_description`, chosen by held-out test score. It never edits the skill.

Reading the outcome:

- An outcome of `error` is not a non-trigger. Surface `error_counts` to the user. A query left with fewer than two non-error runs is reported as `error`, not pass or fail. `outcomes[].reason` says why (`timeout`, `exit code 1`, `permission denied: Bash`).
- An iteration marked `incomplete`, or with `test: "not run (max_total_usd reached)"`, was stopped by the cost cap and is never selected. `exit_reason: "max_total_usd reached"` means the result may not be the best reachable; say so.
- If `best_description` is `null`, nothing was tested.
- Slash-style prompts (`/name args`) leave no `Skill` call in the stream, so trigger detection does not apply to them. Use plain-language queries.

## Cost guide

A rough guide only: the scripts print a calibrated estimate after the canary, and that number wins. At the defaults (adaptive runs, test-set gating, 3 iterations) expect about 100-135 trigger calls, at worst about 180, plus the canary and up to 2 rewrites. With a warm prompt cache:

| Model | Per call, uncached / cached | Whole run |
|---|---|---|
| haiku | about $0.005 | under $1 |
| sonnet | $0.094 / $0.029 | about $2-4 |
| opus | $0.186 / $0.058 | about $4-8, worst case about $11 |

A cold cache on every call costs about 3 times as much. Offer `--max-total-usd` as a safety cap.

## Results

Written to `<skill-name>-workspace/trigger-opt/<timestamp>/` under the current directory (or `--results-dir`):

- `results.json`: full record ([schemas.md](schemas.md#trigger-optimization-resultsjson)).
- `report.html`: per-iteration table of pass and fail by query. Rebuild it with `generate_report.py` (below).
- `logs/`.

Disposable trial projects live in system temp and are deleted on exit and on crash. Keep the results directory; a later session can compare against it. The workspace must stay outside every skills directory.

## 5. Apply the best description

Read `best_description` and `best_selection` from `results.json`. Show the user the old and new descriptions with the train and test scores, and apply it only with their approval.

Apply it by editing only the `description` value in the skill's SKILL.md frontmatter. Leave `when_to_use` and every other key exactly as they are: Claude Code appends `when_to_use` to the description in the listing, and a rewrite that drops it loses triggers. Keep the combined length under 1,536 characters. For a target that also ships to claude.ai or the API, `description` must stay within 1,024 characters. Then validate:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/quick_validate.py <skill-path>
```

Do not write into a skill under `~/.claude/plugins/`: plugin updates overwrite it. Edit the plugin's source repository, or copy the skill to personal or project scope and say so.

## Single-step tools

`run_loop.py` composes these; call them directly to measure one description or rewrite one candidate. All accept the `--` passthrough.

```bash
# Measure one description (default: the skill's own). Same plan / --yes flow.
uv run ${CLAUDE_SKILL_DIR}/scripts/eval/run_eval.py \
  --eval-set <eval-set.json> --skill-path <skill-path> --model <id> \
  [--description "<text>"] [--runs-per-query 3] [--fixed-runs] \
  [--num-workers 5] [--timeout 60] [--max-total-usd <n>] [--yes] [--json]

# Rewrite a description from a run_eval.py --json result. Prints the bare description.
uv run ${CLAUDE_SKILL_DIR}/scripts/eval/improve_description.py \
  --eval-results <run_eval-output.json> --skill-path <skill-path> --model <id> \
  [--history <attempts.json>] [--timeout 180] [--max-total-usd <n>] [--verbose] [--json]

# Build report.html from a results.json (or - for stdin). Prints HTML to stdout without -o.
uv run ${CLAUDE_SKILL_DIR}/scripts/eval/generate_report.py <results.json> [-o <report.html>] [--json]
```

`--history` is a JSON list of earlier attempts: `{description, train_passed, train_total, per_query}`. The rewrite call is tool-free and runs in an isolated directory; it needs no tools.

## CLI version and tests

The last verified CLI version is `2.1.302`. This is informational, not a pin. The verified trigger-run command, with the working directory set to the trial's temp project, is:

```bash
claude -p "<query>" --output-format stream-json --verbose --include-partial-messages \
  --model <model> --no-session-persistence --setting-sources project \
  --strict-mcp-config --tools Skill --permission-mode dontAsk
```

`--tools` replaces the whole built-in set, so `Skill` must be in it or every query scores not-triggered. After a Claude Code upgrade, or when results look wrong, run the offline parser tests, which replay recorded streams and spend no model calls:

```bash
uv run --with pytest --with pyyaml --with click pytest ${CLAUDE_SKILL_DIR}/tests -q
```

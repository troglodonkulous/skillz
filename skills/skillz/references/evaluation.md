# Evaluation

The Evaluate loop: run a skill against realistic prompts with and without it, grade the outputs, aggregate, show a human the results, and iterate. Compare mode (is v2 better than v1?) is at the end. File formats are in [schemas.md](schemas.md); this file does not repeat them.

## Contents

- [Before you start](#before-you-start)
- [The loop](#the-loop)
  - [1. Write the prompts and confirm them](#1-write-the-prompts-and-confirm-them)
  - [2. Choose the workspace and snapshot](#2-choose-the-workspace-and-snapshot)
  - [3. Run: plan first, then spend](#3-run-plan-first-then-spend)
  - [4. Draft expectations while runs are in flight](#4-draft-expectations-while-runs-are-in-flight)
  - [5. Grade each run](#5-grade-each-run)
  - [6. Aggregate](#6-aggregate)
  - [7. Analyst pass](#7-analyst-pass)
  - [8. Generate the viewer](#8-generate-the-viewer)
  - [9. Read feedback and improve](#9-read-feedback-and-improve)
  - [10. Iterate](#10-iterate)
- [Reading run.json](#reading-runjson)
- [Subagent fallback](#subagent-fallback)
- [Compare mode](#compare-mode)

## Before you start

- Evaluate only skills with objectively checkable outputs (files created, data extracted, steps followed). For subjective skills (tone, visual style), say so, recommend qualitative review with the user, and stop.
- The `claude` CLI is the preferred backend. Without it, use the [subagent fallback](#subagent-fallback).
- Every script below is run as `uv run ${CLAUDE_SKILL_DIR}/scripts/eval/<script>.py` and accepts `--help` and `--json`.
- Each `run_tasks.py` run is a full agent session that spends real usage. State the session count (evals x 2 configurations x runs) to the user before step 3's `--yes`.

## The loop

Use this as a todo list. Step 8 comes before you form or state any opinion of the outputs.

### 1. Write the prompts and confirm them

Write 2-3 realistic prompts, the kind a real user would type, to `evals/evals.json` inside the skill directory (shape in [schemas.md](schemas.md#evalsevalsjson)). Each eval needs an integer `id`, a short lowercase-hyphenated `name`, and a `prompt`. Leave `expectations` empty for now.

- The prompt is passed to `claude -p` byte for byte. For a skill the user invokes by slash command, write the prompt as `/skill-name args`, so the run exercises `$ARGUMENTS` substitution.
- Input files go in `files`, as paths relative to the skill root.

Show the prompts to the user and wait for approval. Do not run anything before that.

### 2. Choose the workspace and snapshot

The workspace is `<skill-name>-workspace/`, in the current working directory or a path the user picks. It must be outside every skills directory (`~/.claude/skills/`, a project's `.claude/skills/`, a plugin cache): inside one it clutters skill discovery and, at project scope, gets committed. If the working directory is a skills directory, ask for another location. `run_tasks.py` refuses a workspace inside any skill directory, any `.claude/skills` tree, or a plugin's `skills/` directory.

When improving an existing skill, snapshot it before editing:

```bash
mkdir -p <skill-name>-workspace
cp -r <skill-path> <skill-name>-workspace/skill-snapshot
```

Baseline choice, passed to `run_tasks.py --baseline`:

| Situation | `--baseline` |
|---|---|
| New skill | `no_skill` (default) |
| Improving an existing skill | the snapshot directory path |

Keep the same baseline for every iteration of one effort.

### 3. Run: plan first, then spend

Plan, with no `--yes`. This prints the call count and makes no `claude` call:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/eval/run_tasks.py \
  --evals <skill-path>/evals/evals.json --skill-path <skill-path> \
  --workspace <skill-name>-workspace/iteration-1 \
  [--baseline no_skill|<snapshot-path>] [--runs 1] [--model <id>] \
  [--tools <list>] [--permission-mode <mode>] [--max-total-usd <n>] \
  [-- <claude -p args>]
```

Show the plan to the user. After they confirm, run the same command with `--yes`, in the background, because it blocks until every run ends.

All runs launch together, with-skill and baseline interleaved, never one configuration after the other. Each is an isolated `claude -p` session: its own temp project, only that configuration's skill staged, personal and plugin skills hidden. The runner writes `outputs/`, `stream.jsonl`, `run.json`, `timing.json`, `transcript.md` and `metrics.json` per run, and `eval_metadata.json` per eval.

| Flag | Meaning |
|---|---|
| `--evals FILE` | Path to `evals.json`. Required. |
| `--skill-path DIRECTORY` | Candidate skill directory. Required. |
| `--workspace DIRECTORY` | The iteration directory, e.g. `<skill-name>-workspace/iteration-1`. Its run directories must not already exist. Required. |
| `--baseline` | `no_skill` (default) or a snapshot skill directory. |
| `--runs N` | Runs per eval x configuration. Default 1. Use 3 or more when variance matters. |
| `--model ID` | Model id. Omitted unless given; the actual model is recorded. |
| `--tools LIST` | Tool list. Default is the execution set; `Skill` is always included. |
| `--permission-mode MODE` | Default `acceptEdits`. |
| `--max-total-usd N` | Stop launching new runs once cumulative cost crosses `N`. |
| `--yes` | Confirm and spend. |
| `--json` | Machine-readable output. |

Everything after a literal `--` goes to `claude -p` verbatim, in order. Use it for permissions the run needs. Under the default `acceptEdits`, file edits and simple filesystem commands in the project (`mkdir`, redirects) are allowed; anything else that would prompt (most Bash, network) is denied, and user-level permission rules are not inherited:

```bash
... --yes -- --allowedTools 'Bash(uv run *)'
```

Passthrough rules:

- Flags the runner sets itself (`--print`, `--output-format`, `--verbose`, `--include-partial-messages`, `--setting-sources`, `--no-session-persistence`, `--strict-mcp-config`), flags that leave the one-shot session (`--continue`, `--resume`, `--worktree`, `--bg`, and similar), and flags that hide the skill (`--bare`, `--safe-mode`, `--disable-slash-commands`) are rejected before any call, with the reason. Use the script's own `--model`, `--tools` and `--permission-mode` instead of passing those through.
- `--add-dir` is allowed, but only with an absolute path: every call runs in a temp project, and relative paths are refused. A directory that holds `.claude/skills/<name>` exposes that skill to the run and can contaminate a baseline.
- `--append-system-prompt` is merged after the runner's harness text, which asks for deliverables in `outputs/` and for `outputs/user_notes.md`.
- A flag the runner has never seen passes through unchanged.

If `--max-total-usd` stops the batch, the summary says how many runs were not launched. Aggregate what exists, or rerun the remainder in a new iteration directory.

### 4. Draft expectations while runs are in flight

While the background command runs, write `expectations` for each eval: verifiable statements that pass when the skill really did the work and fail when it did not. Explain to the user what each one checks. Prefer objective checks (a file exists and has the right content, a script was used) over taste.

`run_tasks.py` copies `expectations` into `eval_metadata.json` when it starts, and the grader reads `eval_metadata.json`. So write the finished list to both `evals/evals.json` and each `iteration-N/eval-<N>-<slug>/eval_metadata.json`.

### 5. Grade each run

`timing.json` is already written by the runner, so no manual capture is needed.

When `run_tasks.py` finishes, read each run's `run.json` ([below](#reading-runjson)). Skip grading runs with `status` `error`: the aggregator counts them as failed. Report them to the user with the `error` reason.

For every other run, spawn a grader subagent that follows [../agents/grader.md](../agents/grader.md) on that run directory. It writes `<run-dir>/grading.json`. Grade runs in parallel, in a single turn. For an expectation checkable by code, tell the grader to write a script instead of judging by eye.

A run with no `grading.json` is reported by the aggregator as failed, not silently dropped.

### 6. Aggregate

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/eval/aggregate_benchmark.py \
  <skill-name>-workspace/iteration-N --skill-name <name> [--skill-path <abs-path>] [--json]
```

`--skill-name` is required: it identifies contaminated baseline runs. The script writes `benchmark.json` and `benchmark.md` into the iteration directory; with `--json` it prints the benchmark instead of a summary. The delta is `with_skill` minus `without_skill`, by role.

Read the result before trusting it:

- `metadata.run_counts` gives `{discovered, succeeded, failed, contaminated}` per eval x configuration. Only succeeded runs enter the means and the delta. Failed and contaminated runs are listed with `status_reason`. Tell the user how many there were and why.
- A `contaminated` baseline could see the candidate skill. Do not report a delta built on one. Find the cause (usually an `--add-dir` or a same-named skill) and rerun.
- `tokens: null` means no token count was recorded; do not substitute another number.
- A `metadata.token_provenance` with more than one real value means two backends were mixed. Their token counts are not comparable; rerun with one backend.

### 7. Analyst pass

Spawn an analyzer subagent in benchmark-notes mode, following [../agents/analyzer.md](../agents/analyzer.md) (the "Analyzing Benchmark Results" section). It surfaces expectations that never discriminate, flaky evals, and time or token tradeoffs, as observations only. Append its notes to `notes` in `benchmark.json` (the viewer reads them there). `benchmark.md` will not contain them, and rerunning `aggregate_benchmark.py` overwrites both files and drops them, so aggregate again only before this step.

### 8. Generate the viewer

Generate the review viewer before you form or state your own assessment of the outputs. The human sees the artifacts first; your opinion comes after. An early verdict from you anchors the reviewer.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/eval/generate_review.py \
  <skill-name>-workspace/iteration-N --skill-name <name> \
  --benchmark <skill-name>-workspace/iteration-N/benchmark.json \
  [--previous-workspace <skill-name>-workspace/iteration-N-1] \
  [--port 3117] [--static <out.html>] [--json]
```

- It starts a local server and blocks, so run it in the background and give the user the URL it prints. If the port is busy it uses a free one; it never kills another process.
- With no browser (headless, remote), use `--static <out.html>` to write a standalone file, and tell the user where it is. Text outputs render offline. A static viewer cannot save feedback to the workspace, so it downloads `feedback.json`; ask the user to put that file in the iteration directory.
- `--previous-workspace` shows the last iteration's outputs and feedback alongside.

Tell the user the viewer has two tabs, Outputs (one run at a time, with a feedback box) and Benchmark (pass rate, time and tokens), and ask them to review every run and submit.

### 9. Read feedback and improve

When the user says they are done, read `iteration-N/feedback.json` ([schemas.md](schemas.md#feedbackjson)). An empty `feedback` string means the run looked fine; a run with no entry was not reviewed. Then give your own assessment alongside theirs.

Improve by generalizing, not by overfitting to three prompts:

- Fix the cause the feedback points to. If the same wrong reasoning appears in several runs, change the instruction, not the single example.
- Read the transcripts. If every run independently wrote the same helper script, bundle that script in the skill.
- Explain why in the instructions rather than adding rigid rules.

### 10. Iterate

Edit the skill, then rerun into a new directory with the same baseline:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/eval/run_tasks.py \
  --evals <skill-path>/evals/evals.json --skill-path <skill-path> \
  --workspace <skill-name>-workspace/iteration-2 --baseline <same-baseline> --yes
```

Repeat steps 5-9, passing `--previous-workspace` to the viewer. Stop when the user is happy, feedback is empty, or progress has stalled. For a skill that will be shared, offer [trigger-optimization.md](trigger-optimization.md) afterwards.

## Reading run.json

Each CLI run has a `run.json` ([schemas.md](schemas.md#runjson)). The fields that change what you do:

| Field | What to do |
|---|---|
| `status: "error"` | Excluded from the benchmark as failed. `error` says why (`timeout`, `no result event`, `init.skills unavailable ...`). Fix the cause and rerun; do not grade it. |
| `status: "contaminated"` or `contaminated: true` | A baseline that could see the candidate. Excluded from the delta. Do not report it as a result. |
| `timed_out: true` | The runner killed the session. The outputs are partial. |
| `permission_denials` non-empty, or `permission_denied_events > 0` | The run needed a tool it was not allowed, so it is an error. Rerun with the permission after `--`, e.g. `--allowedTools 'Bash(uv run *)'`. |
| `warnings` | Read them. For example, a passthrough that bypasses permission checks weakens the isolation; tell the user. |
| `init.skills` | The skills the run could see. The basis of the contamination check. |

Do not judge a run by `result.subtype`: an auth failure still reports `success`.

## Subagent fallback

Use only when the `claude` CLI is unavailable. Tell the user: runtime features (argument substitution, hooks, permissions, forking) were not exercised, and contamination could not be checked because subagents can still discover installed skills.

For each eval, spawn the with-skill and baseline subagents in the same turn, never one configuration after the other. Create the layout from [schemas.md](schemas.md#workspace-layout) by hand, including `eval_metadata.json` and the `run-1/` level. There is no `run.json` or `stream.jsonl`, so the agent writes everything else. Each subagent prompt must ask explicitly for:

- the deliverables, saved to `run-N/outputs/`;
- `transcript.md`, with an `## Eval Prompt` section holding the verbatim prompt, then the steps taken;
- `metrics.json` (tool counts, files created, errors);
- `outputs/user_notes.md`, the subagent's own uncertainties and workarounds.

The completion notification ends with a `<usage>` block containing `subagent_tokens`, `tool_uses` and `duration_ms`. This is the only moment those numbers exist, so write `timing.json` for the run immediately:

```json
{"total_tokens": <subagent_tokens>, "duration_ms": <duration_ms>,
 "total_duration_seconds": <duration_ms / 1000, one decimal>,
 "usage": null, "provenance": "subagent_notification"}
```

If the block is absent, record the wall-clock duration with `"total_tokens": null` and `"provenance": "wall_clock"`. Never mix backends within one benchmark: subagent token counts and CLI counts are not comparable. If a benchmark already has CLI runs, finish it with the CLI.

Grading, aggregation, analysis and the viewer are the same as above. The aggregator cannot check contamination without `run.json`, so say that in the report.

## Compare mode

Use when the user asks whether one version is actually better than another and a pass-rate delta is not enough.

1. Run both versions on the same prompts through the normal loop: `--skill-path` the newer version, `--baseline` the older one's directory. Both outputs then come from the same eval.
2. For each eval, spawn a comparator subagent per [../agents/comparator.md](../agents/comparator.md). Give it the two output directories labeled A and B in random order, the eval prompt, the expectations, and an `output_path` of `<iteration-dir>/<eval-dir>/comparison-N.json`. Do not tell it which version produced which. Keep your own record of which letter was which version.
3. Read each `winner`. On `TIE`, report the tie with the comparator's reasoning and skip the analyzer for that eval: there is no winner to explain. Otherwise unblind: spawn an analyzer in post-hoc mode, per [../agents/analyzer.md](../agents/analyzer.md) (the "Post-hoc Analyzer Agent" section), with the winner, the comparator's result path, both skill paths, both transcripts and an `output_path` of `<iteration-dir>/<eval-dir>/analysis.json`. It explains why the winner won and proposes prioritized improvements.
4. Report the winner per eval, the comparator's reasoning, and the analyzer's top suggestions. Generate the viewer before stating your own view, as in step 8.

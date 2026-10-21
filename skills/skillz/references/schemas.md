# Data contracts

Every JSON file the skillz tools exchange, with field names, types, nullability, the tool that writes it, and the tool that reads it. Tools implement exactly this document. The viewer reads some names literally, so renaming a field breaks it silently.

## Contents

- [Conventions](#conventions)
- [Workspace layout](#workspace-layout)
- [evals/evals.json](#evalsevalsjson)
- [eval_metadata.json](#eval_metadatajson)
- [run.json](#runjson)
- [timing.json](#timingjson)
- [metrics.json](#metricsjson)
- [transcript.md](#transcriptmd)
- [outputs/user_notes.md](#outputsuser_notesmd)
- [grading.json](#gradingjson)
- [benchmark.json](#benchmarkjson)
- [benchmark.md](#benchmarkmd)
- [feedback.json](#feedbackjson)
- [Trigger eval set](#trigger-eval-set)
- [Trigger-optimization results.json](#trigger-optimization-resultsjson)
- [comparison.json](#comparisonjson)
- [analysis.json](#analysisjson)

---

## Conventions

- The checks vocabulary is `expectations`, in every file and in prose.
- `null` means "not measured or not recorded". It is never replaced by `0`, an empty string, or a placeholder such as `"<model-name>"`.
- Timestamps are ISO 8601 UTC strings (`2026-10-14T10:30:00Z`).
- "Writer" is the tool or agent that creates the file. "Reader" is every tool that parses it.
- Tool names: `run_tasks.py` (execution runner), `aggregate_benchmark.py` (aggregator), `generate_review.py` (viewer server) with `viewer.html`, `run_eval.py` / `run_loop.py` / `improve_description.py` (trigger optimization), `grader` / `analyzer` / `comparator` (subagents, see `agents/`).

---

## Workspace layout

The workspace is `<skill-name>-workspace/`, in the current working directory or a user-chosen path, never inside a skills directory.

```
<skill-name>-workspace/
├── skill-snapshot/                       # only when improving an existing skill
├── iteration-1/
│   ├── eval-1-preserves-formulas/        # eval-<N>-<slug>/
│   │   ├── eval_metadata.json
│   │   ├── with_skill/run-1/{outputs/, transcript.md, stream.jsonl, run.json, timing.json, metrics.json, grading.json}
│   │   └── without_skill/run-1/{...same...}
│   ├── benchmark.json
│   ├── benchmark.md
│   └── feedback.json
└── trigger-opt/<timestamp>/              # only when trigger optimization ran
    ├── results.json
    ├── report.html
    └── logs/
```

Rules:

- `eval-<N>-<slug>/`: `<N>` is the integer `eval_id`; `<slug>` is the lowercase-hyphenated `eval_name`. The aggregator globs `eval-*`.
- `with_skill` and `without_skill` are the only configuration directory names. A snapshot baseline is recorded as `baseline_kind: "snapshot"` in `eval_metadata.json`, never by renaming the directory.
- The `run-N/` level is mandatory, even for one run. A configuration directory without a `run-*` child is ignored by the aggregator. `N` is a positive integer.
- `eval_metadata.json` lives at the eval directory level (two levels above a run directory). Readers find it by searching upward from the run directory to the eval directory.
- `outputs/` holds the run's deliverables, plus `outputs/user_notes.md`. `stream.jsonl`, `run.json` exist only for runs executed by `run_tasks.py`. Under the subagent fallback they are absent and the agent writes the rest.
- Benchmarks live in `iteration-N/`, not `benchmarks/<timestamp>/`.
- There is no `history.json`. The iteration directories are the history.

---

## evals/evals.json

The eval definitions for a skill. Located at `evals/evals.json` inside the skill directory. Writer: the skill author, with Claude. Readers: the eval workflow when creating `eval_metadata.json`; `run_tasks.py` (prompt and files).

```json
{
  "skill_name": "example-skill",
  "evals": [
    {
      "id": 1,
      "name": "preserves-formulas",
      "prompt": "User's example prompt",
      "expected_output": "Description of expected result",
      "files": ["evals/files/sample1.xlsx"],
      "expectations": [
        "The output includes X",
        "The skill used script Y"
      ]
    }
  ]
}
```

| Field | Type | Null | Meaning |
|---|---|---|---|
| `skill_name` | string | no | Matches the skill's frontmatter `name`. |
| `evals[].id` | integer | no | Unique within the file. Becomes `eval_id`. |
| `evals[].name` | string | no | Short lowercase-hyphenated slug. Becomes `eval_name` and the `<slug>` in the directory name. |
| `evals[].prompt` | string | no | The task, passed verbatim as the `claude -p` prompt (byte-identical). |
| `evals[].expected_output` | string | no | Human-readable description of success. |
| `evals[].files` | string[] | no, may be `[]` | Input files, paths relative to the skill root. |
| `evals[].expectations` | string[] | no, may be `[]` | Verifiable statements. Drafted while runs are in flight, so may be empty at first. |

---

## eval_metadata.json

Single source of truth per eval inside a workspace. Located at `iteration-N/eval-<N>-<slug>/eval_metadata.json`. Writer: the eval workflow (copied from `evals.json` plus baseline fields). Readers: `run_tasks.py`, `aggregate_benchmark.py`, `generate_review.py`, `grader`.

```json
{
  "eval_id": 1,
  "eval_name": "preserves-formulas",
  "prompt": "...",
  "expected_output": "...",
  "files": [],
  "expectations": [],
  "baseline_kind": "no_skill",
  "baseline_label": "no skill"
}
```

| Field | Type | Null | Meaning |
|---|---|---|---|
| `eval_id` | integer | no | Same as `evals[].id`. |
| `eval_name` | string | no | Same as `evals[].name`. Propagates to `benchmark.json` `runs[].eval_name`, the viewer's section header. |
| `prompt` | string | no | The verbatim eval prompt. The viewer shows it. |
| `expected_output` | string | no | As in `evals.json`. |
| `files` | string[] | no | As in `evals.json`. |
| `expectations` | string[] | no | The checks the grader grades against. |
| `baseline_kind` | `"no_skill"` \| `"snapshot"` | no | What `without_skill` ran against. |
| `baseline_label` | string | no | Display label for the baseline: `"no skill"`, or e.g. `"v1 (original)"` for a snapshot. |

---

## run.json

The runner's record of what actually happened in one run. Located at `<run-dir>/run.json`. Writer: `run_tasks.py`. Readers: `aggregate_benchmark.py` (run status, contamination, `executor_model`), `generate_review.py`. Absent under the subagent fallback.

```json
{
  "configuration": "without_skill",
  "exit_code": 0,
  "stderr_tail": "",
  "is_error": false,
  "terminal_reason": "completed",
  "permission_denials": [],
  "permission_denied_events": 0,
  "init": {
    "skills": ["pdf", "docx"],
    "model": "claude-sonnet-4-5",
    "tools": ["Skill", "Read", "Write"],
    "permission_mode": "acceptEdits"
  },
  "cli_version": "2.1.300",
  "argv": ["claude", "-p", "<prompt>", "--output-format", "stream-json", "..."],
  "extra_args": ["--allowedTools", "Bash(touch *)"],
  "permission_mode": "acceptEdits",
  "added_flags": ["--tools Skill,Read,Write (Skill appended)"],
  "total_cost_usd": 0.0231,
  "num_turns": 4,
  "contaminated": false,
  "status": "ok",
  "error": null,
  "timed_out": false,
  "warnings": []
}
```

| Field | Type | Null | Meaning |
|---|---|---|---|
| `configuration` | `"with_skill"` \| `"without_skill"` | no | Role of this run. |
| `exit_code` | integer | no | Process exit code. |
| `stderr_tail` | string | no | Last lines of stderr (may be empty). |
| `is_error` | boolean | yes | `result.is_error`. `null` when no `result` event was emitted. |
| `terminal_reason` | string | yes | `result.terminal_reason`. |
| `permission_denials` | array | no | `result.permission_denials`, verbatim. |
| `permission_denied_events` | integer | no | Count of `system`/`permission_denied` events in the stream. |
| `init.skills` | string[] | yes | Flat skill-name array from the `system`/`init` event. `null` when no `init` event was seen. |
| `init.model` | string | yes | `init.model`. Source of benchmark `executor_model`. |
| `init.tools` | string[] | yes | `init.tools`. |
| `init.permission_mode` | string | yes | `init.permissionMode`. |
| `cli_version` | string | yes | `init.claude_code_version`. |
| `argv` | string[] | no | The complete argv, including the prompt and passthrough. |
| `extra_args` | string[] | no | The caller's passthrough after `--`, verbatim and in order. |
| `permission_mode` | string | no | The mode the runner passed. |
| `added_flags` | string[] | no | Flags the runner changed or appended (e.g. `Skill` appended to `--tools`). |
| `total_cost_usd` | number | yes | `result.total_cost_usd`. |
| `num_turns` | integer | yes | `result.num_turns`. |
| `contaminated` | boolean | no | The runner's contamination verdict. `true` only for a `without_skill` run that could see the candidate: `init.skills` lists it, or, for a snapshot baseline with the candidate's own name, `init.skills` lists the name twice or an `--add-dir` exposes `.claude/skills/<name>`. |
| `status` | `"ok"` \| `"error"` \| `"contaminated"` | no | The runner's verdict for this run. `error` also covers a timeout, a missing `result` event, a `without_skill` run whose `init.skills` is `null` (isolation unverifiable), a failed output copy, and a runner failure. |
| `error` | string | yes | Why `status` is `error` (e.g. `timeout`, `no result event`, `init.skills unavailable; baseline isolation could not be verified`); `null` otherwise. |
| `timed_out` | boolean | no | The runner killed the session at its timeout. |
| `warnings` | string[] | no | Runner warnings about the call, e.g. that passthrough bypasses permission checks. |
| `output_copy_error` | string | optional | Present only when copying `outputs/` out of the temp project failed. |
| `runner_failure` | string | optional | Present only when the runner itself raised; the other fields may then be `null`. |

A run is a **run error** when `status` is `"error"`, or `exit_code != 0`, or `is_error` is `true`, or `permission_denials` is non-empty, or `permission_denied_events > 0`. A run is **contaminated** when `contaminated` is `true`; readers trust this verdict over their own name check, because only the runner knows about same-name snapshot baselines. Never classify from `result.subtype`: an auth failure reports `subtype: "success"`.

---

## timing.json

Timing and token usage for a run. Located at `<run-dir>/timing.json`. Writer: `run_tasks.py` (from the `result` event); under the subagent fallback, the agent running the task writes it immediately from the task notification, since that data is not persisted anywhere else. Readers: `aggregate_benchmark.py`, `grader`.

```json
{
  "total_tokens": 84852,
  "duration_ms": 23332,
  "total_duration_seconds": 23.3,
  "usage": {
    "input_tokens": 1200,
    "output_tokens": 3400,
    "cache_creation_input_tokens": 20000,
    "cache_read_input_tokens": 60252
  },
  "provenance": "cli_result",
  "executor_start": "2026-01-15T10:30:00Z",
  "executor_end": "2026-01-15T10:32:45Z",
  "executor_duration_seconds": 165.0,
  "grader_start": "2026-01-15T10:32:46Z",
  "grader_end": "2026-01-15T10:33:12Z",
  "grader_duration_seconds": 26.0
}
```

| Field | Type | Null | Meaning |
|---|---|---|---|
| `total_tokens` | integer | yes | Under `cli_result`: exactly `input_tokens + output_tokens + cache_creation_input_tokens + cache_read_input_tokens`. Under `subagent_notification`: the notification's `subagent_tokens`. Under `wall_clock`: `null`. |
| `duration_ms` | integer | no | Run duration in milliseconds. `result.duration_ms` for `cli_result`; the notification's `duration_ms` for `subagent_notification`; measured wall clock for `wall_clock`. |
| `total_duration_seconds` | number | no | `duration_ms / 1000`, rounded to 0.1. The aggregator reads this for `time_seconds`. |
| `usage` | object | yes | The four usage fields above. `null` unless provenance is `cli_result`: the subagent notification does not expose a breakdown. |
| `provenance` | `"cli_result"` \| `"subagent_notification"` \| `"wall_clock"` | no | Where `total_tokens` and `duration_ms` came from. |
| `executor_*`, `grader_*` | string / number | yes | Optional phase timestamps and durations, as in earlier versions. |

`tokens: null` rules:

- `total_tokens` is `null` exactly when provenance is `wall_clock`, or when the source did not report it.
- The aggregator copies `timing.json` `total_tokens` into `runs[].result.tokens` and records `tokens_provenance` next to it. If `timing.json` is missing or has no `total_tokens`, `tokens` is `null` and `tokens_provenance` is `"missing"` or `"wall_clock"`. Never fall back to `output_chars`, a character count, or `0`.
- One benchmark never mixes `cli_result` and `subagent_notification` token counts. The aggregator reports the provenance set in `metadata.token_provenance` and flags a mix in `notes`.

---

## metrics.json

Tool usage and output size for a run. Located at `<run-dir>/metrics.json`. Writer: `run_tasks.py` renders it from `stream.jsonl`; under the subagent fallback the executor agent writes it. Readers: `grader` (it becomes `grading.json` `execution_metrics`), `aggregate_benchmark.py` (via `grading.json`).

```json
{
  "tool_calls": {"Read": 5, "Write": 2, "Bash": 8, "Edit": 1, "Glob": 2, "Grep": 0},
  "total_tool_calls": 18,
  "total_steps": 6,
  "files_created": ["filled_form.pdf", "field_values.json"],
  "errors_encountered": 0,
  "output_chars": 12450,
  "transcript_chars": 3200
}
```

| Field | Type | Meaning |
|---|---|---|
| `tool_calls` | object (string to integer) | Count per tool name. |
| `total_tool_calls` | integer | Sum of `tool_calls`. |
| `total_steps` | integer | Assistant turns. |
| `files_created` | string[] | Files under `outputs/`, relative to it. |
| `errors_encountered` | integer | Tool results flagged as errors. |
| `output_chars` | integer | Total characters of text output files. A size measure only, never a token count. |
| `transcript_chars` | integer | Characters in `transcript.md`. |

---

## transcript.md

Human-readable rendering of the run. Located at `<run-dir>/transcript.md`. Writer: `run_tasks.py`, rendered from `stream.jsonl` (under the subagent fallback, the executor writes it). Readers: `grader`, `generate_review.py` (prompt fallback), humans.

It renders, in order: an `## Eval Prompt` section holding the verbatim prompt (the viewer's fallback parses this heading), then each assistant text block, each tool call with its arguments, and each tool result (truncated if long), then the final result line. It is markdown, not JSON; no tool parses anything else out of it.

---

## outputs/user_notes.md

The executor's own account of uncertainties, workarounds, and anything needing human review. Located at `<run-dir>/outputs/user_notes.md`. Writer: the executed `claude -p` session or subagent, asked by the runner's harness text. Readers: `grader` (summarised into `grading.json` `user_notes_summary`), humans. Free-form markdown. It may be missing; the grader then records an empty `user_notes_summary`. The viewer excludes `user_notes.md`, `transcript.md`, and `metrics.json` from the displayed output files.

---

## grading.json

Output from the grader agent. Located at `<run-dir>/grading.json`. Writer: `grader`. Readers: `aggregate_benchmark.py`, `generate_review.py`, `viewer.html`.

```json
{
  "expectations": [
    {
      "text": "The output includes the name 'John Smith'",
      "passed": true,
      "evidence": "Found in transcript Step 3: 'Extracted names: John Smith, Sarah Johnson'"
    },
    {
      "text": "The spreadsheet has a SUM formula in cell B10",
      "passed": false,
      "evidence": "No spreadsheet was created. The output was a text file."
    }
  ],
  "summary": {"passed": 1, "failed": 1, "total": 2, "pass_rate": 0.5},
  "execution_metrics": {
    "tool_calls": {"Read": 5, "Write": 2, "Bash": 8},
    "total_tool_calls": 15,
    "total_steps": 6,
    "errors_encountered": 0,
    "output_chars": 12450,
    "transcript_chars": 3200
  },
  "timing": {
    "executor_duration_seconds": 165.0,
    "grader_duration_seconds": 26.0,
    "total_duration_seconds": 191.0
  },
  "claims": [
    {"claim": "The form has 12 fillable fields", "type": "factual", "verified": true, "evidence": "Counted 12 fields in field_info.json"}
  ],
  "user_notes_summary": {
    "uncertainties": ["Used 2023 data, may be stale"],
    "needs_review": [],
    "workarounds": ["Fell back to text overlay for non-fillable fields"]
  },
  "eval_feedback": {
    "suggestions": [
      {"expectation": "The output includes the name 'John Smith'", "reason": "A hallucinated document that mentions the name would also pass"}
    ],
    "overall": "Expectations check presence but not correctness."
  }
}
```

| Field | Type | Null | Meaning |
|---|---|---|---|
| `expectations[]` | array | no | One item per expectation in `eval_metadata.json`, in order. Items use exactly `text` (string), `passed` (boolean), `evidence` (string). The viewer reads these three names only. |
| `summary` | object | no | `passed`, `failed`, `total` (integers) and `pass_rate` (number 0 to 1, `passed / total`; `0.0` when `total` is 0). |
| `execution_metrics` | object | yes | Copy of `metrics.json`. |
| `timing` | object | yes | `executor_duration_seconds`, `grader_duration_seconds`, `total_duration_seconds`. The aggregator prefers `timing.json` and uses this only for `time_seconds` when `timing.json` has no duration. |
| `claims[]` | array | no, may be `[]` | `claim`, `type` (`factual` \| `process` \| `quality`), `verified` (boolean), `evidence`. |
| `user_notes_summary` | object | no | `uncertainties`, `needs_review`, `workarounds`: string arrays. The aggregator flattens all three into `runs[].notes`. |
| `eval_feedback` | object | optional | Present only when the grader found a real gap. `suggestions[]` items have `reason` (string) and an optional `expectation` (string: the existing expectation text the suggestion is about; omitted when the suggestion is an uncovered outcome). `overall` is a string. |

A `grading.json` that is missing, not valid JSON, or has no `expectations` array makes the run `failed`.

---

## benchmark.json

Aggregate of one iteration. Located at `iteration-N/benchmark.json`. Writer: `aggregate_benchmark.py` (the `notes` array is filled by the `analyzer` in benchmark mode). Readers: `viewer.html` (via `generate_review.py --benchmark`), `benchmark.md` generation.

```json
{
  "metadata": {
    "skill_name": "pdf",
    "skill_path": "/path/to/pdf",
    "executor_model": "claude-sonnet-4-5",
    "analyzer_model": null,
    "timestamp": "2026-01-15T10:30:00Z",
    "evals_run": [1, 2, 3],
    "baseline_kind": "snapshot",
    "baseline_label": "v1 (original)",
    "token_provenance": ["cli_result"],
    "run_counts": [
      {"eval_id": 1, "eval_name": "ocean", "configuration": "with_skill", "discovered": 3, "succeeded": 3, "failed": 0, "contaminated": 0},
      {"eval_id": 1, "eval_name": "ocean", "configuration": "without_skill", "discovered": 1, "succeeded": 0, "failed": 1, "contaminated": 0}
    ]
  },

  "runs": [
    {
      "eval_id": 1,
      "eval_name": "ocean",
      "configuration": "with_skill",
      "run_number": 1,
      "status": "succeeded",
      "status_reason": null,
      "result": {
        "pass_rate": 0.85,
        "passed": 6,
        "failed": 1,
        "total": 7,
        "time_seconds": 42.5,
        "tokens": 3800,
        "tokens_provenance": "cli_result",
        "tool_calls": 18,
        "errors": 0
      },
      "expectations": [{"text": "...", "passed": true, "evidence": "..."}],
      "notes": ["Used 2023 data, may be stale"]
    }
  ],

  "run_summary": {
    "with_skill": {
      "pass_rate": {"mean": 0.85, "stddev": 0.05, "min": 0.80, "max": 0.90},
      "time_seconds": {"mean": 45.0, "stddev": 12.0, "min": 32.0, "max": 58.0},
      "tokens": {"mean": 3800, "stddev": 400, "min": 3200, "max": 4100}
    },
    "without_skill": {
      "pass_rate": {"mean": 0.35, "stddev": 0.08, "min": 0.28, "max": 0.45},
      "time_seconds": {"mean": 32.0, "stddev": 8.0, "min": 24.0, "max": 42.0},
      "tokens": null
    },
    "delta": {"pass_rate": "+0.50", "time_seconds": "+13.0", "tokens": null}
  },

  "notes": ["Eval 3 shows high variance (50% +/- 40%): may be flaky or model-dependent"]
}
```

**`metadata`**

| Field | Type | Null | Meaning |
|---|---|---|---|
| `skill_name` | string | no | From `evals.json` / frontmatter. |
| `skill_path` | string | no | Absolute path of the skill. |
| `executor_model` | string | yes | `init.model` from the runs' `run.json`; `null` when no `run.json` has one. If runs disagree, the most common value, and `notes` says so. |
| `analyzer_model` | string | yes | `null` unless the analyzer's model was recorded. Never a placeholder. |
| `timestamp` | string | no | When aggregated. |
| `evals_run` | integer[] | no | `eval_id`s found, sorted. Displayed by the viewer. |
| `baseline_kind` | `"no_skill"` \| `"snapshot"` | no | From `eval_metadata.json` (identical across evals; the aggregator errors if they differ). |
| `baseline_label` | string | no | From `eval_metadata.json`. The viewer shows it in place of "Without Skill" when present. |
| `token_provenance` | string[] | no | Distinct `timing.json` provenances across succeeded runs, plus `"missing"` if any run lacked `timing.json`. More than one real provenance is flagged in `notes`. |
| `run_counts[]` | array | no | One row per eval × configuration found. `discovered` = number of `run-*` directories; `succeeded + failed + contaminated == discovered`. Replaces the removed `runs_per_configuration`, which must not appear. The viewer header derives its run-count text from these rows (for example `with_skill 3/3, without_skill 0/1 succeeded`). |

**`runs[]`**: one item per discovered `run-N/` directory, failed and contaminated included, never dropped. Ordered by `eval_id`, then `with_skill` before `without_skill`, then `run_number`. Role order matters because the viewer colours configurations by first appearance.

| Field | Type | Null | Meaning |
|---|---|---|---|
| `eval_id` | integer | no | From `eval_metadata.json`; if absent, parsed from the `eval-<N>-` directory name. |
| `eval_name` | string | yes | From `eval_metadata.json`. The viewer's section header; it falls back to `Eval <id>` when null. |
| `configuration` | `"with_skill"` \| `"without_skill"` | no | The viewer groups and colours by this exact string. |
| `run_number` | integer | no | `N` of `run-N`. |
| `status` | `"succeeded"` \| `"failed"` \| `"contaminated"` | no | See below. |
| `status_reason` | string | yes | `null` when `succeeded`; otherwise a short reason, e.g. `grading.json missing`, `grading.json is not valid JSON`, `run.json: is_error true`, `permission denied: Write`, `init.skills contains candidate "pdf"`. |
| `result` | object | yes | Nested; the viewer reads `result.pass_rate`, `result.time_seconds`, `result.errors`. `null` when `grading.json` could not be read. Present (but excluded from aggregates) for `contaminated` runs. |
| `result.pass_rate`, `passed`, `failed`, `total` | number / integer | no | From `grading.json` `summary`. |
| `result.time_seconds` | number | yes | `timing.json` `total_duration_seconds`; falls back to `grading.json` `timing.total_duration_seconds`; else `null`. |
| `result.tokens` | integer | yes | `timing.json` `total_tokens` or `null`. |
| `result.tokens_provenance` | string | no | `timing.json` `provenance`, or `"missing"` when there is no `timing.json`. |
| `result.tool_calls` | integer | yes | `grading.json` `execution_metrics.total_tool_calls`, else `null`. |
| `result.errors` | integer | yes | `grading.json` `execution_metrics.errors_encountered`, else `null`. |
| `expectations` | array | no | The `text` / `passed` / `evidence` items from `grading.json`; `[]` when unreadable. |
| `notes` | string[] | no | Flattened `user_notes_summary`; `[]` when none. |

**Run status.** Checked in this order; the first match wins, so the counts are disjoint:

1. `failed`: `grading.json` missing, malformed, or without an `expectations` array; or `run.json` shows a run error (see [run.json](#runjson)).
2. `contaminated`: `configuration` is `without_skill` and `run.json` `init.skills` contains the candidate `skill_name`. Never applies to `with_skill`. A missing `run.json` (subagent fallback) cannot be checked, so the run is `succeeded`.
3. `succeeded`: otherwise.

**`run_summary`**: keys are exactly `with_skill`, `without_skill`, `delta`. The viewer treats every key except `delta` as a configuration and reads them in insertion order, so `with_skill` comes first. Statistics (`mean`, `stddev`, `min`, `max`) are computed over **succeeded runs only**, over non-null values. A configuration with no succeeded run for a metric has that metric (or the whole configuration entry for `pass_rate`) as `null`, never zeros.

**`delta`** = `with_skill - without_skill` of the means, taken by declared role and never by directory order. Values are signed strings with fixed precision (`pass_rate` `"%+.2f"`, `time_seconds` `"%+.1f"`, `tokens` `"%+.0f"`). A delta is `null` when either side lacks a mean for that metric.

**`notes`**: observations from the analyzer plus aggregator warnings (mixed token provenance, differing `executor_model`, failed or contaminated runs). Freeform strings.

**Viewer reads exactly** (do not rename): `metadata.skill_name`, `timestamp`, `evals_run`, `baseline_label`, `run_counts`; `run_summary.<config>.{pass_rate,time_seconds,tokens}.{mean,stddev}` and `run_summary.delta.*`; `runs[].eval_id`, `eval_name`, `configuration`, `run_number`, `status`, `status_reason`, `result.{pass_rate,time_seconds,errors}`, `expectations[].{text,passed,evidence}`; `notes`. The viewer must exclude runs whose `status` is not `succeeded` from per-eval averages, and show their `status` and `status_reason`.

---

## benchmark.md

Human-readable rendering of `benchmark.json`. Located at `iteration-N/benchmark.md`. Writer: `aggregate_benchmark.py`, from the same in-memory data as `benchmark.json`. Reader: humans and the `analyzer`.

Contents, in order: title with `skill_name`; `executor_model` (or `unknown`), timestamp, evals; a run-count table with one row per eval × configuration (`discovered`, `succeeded`, `failed`, `contaminated`); a summary table with the metrics (mean ± stddev) for `with_skill` first and the baseline second (labelled `baseline_label`), then the delta column; token provenance; a list of failed and contaminated runs with `status_reason`; `notes`. A `null` statistic renders as `n/a`. It never contains the text `runs_per_configuration`.

---

## feedback.json

Human review feedback. Located at `iteration-N/feedback.json`. Writer: the viewer (`viewer.html` POSTs to `generate_review.py` `/api/feedback`, which writes the file; the static viewer downloads it). Readers: `generate_review.py` (as `--previous-feedback` context), Claude.

```json
{
  "reviews": [
    {"run_id": "eval-1-preserves-formulas-with_skill-run-1", "feedback": "the chart is missing axis labels", "timestamp": "2026-01-15T10:40:00Z"}
  ],
  "status": "complete"
}
```

| Field | Type | Meaning |
|---|---|---|
| `reviews[].run_id` | string | The run directory path relative to the viewer's workspace root, with path separators replaced by `-` (`eval-1-slug-with_skill-run-1`). The viewer derives it; it contains the configuration name. |
| `reviews[].feedback` | string | The reviewer's text. An empty string means "looked fine"; a run with no entry was not reviewed. |
| `reviews[].timestamp` | string | When saved. |
| `status` | `"in_progress"` \| `"complete"` | `complete` is set on final submit and includes every run, even those without feedback. |

---

## Trigger eval set

The queries used to measure whether a description triggers. Any path the user chooses (default `<skill-name>-workspace/trigger-opt/eval-set.json`). Writer: Claude, after the user reviews the set. Readers: `run_eval.py`, `run_loop.py`.

```json
[
  {"id": "q1", "query": "ok so my boss sent me this xlsx (downloads/q4 sales final v2.xlsx) and wants a pivot summary", "should_trigger": true},
  {"query": "write a fibonacci function in python", "should_trigger": false}
]
```

| Field | Type | Null | Meaning |
|---|---|---|---|
| `query` | string | no | The user prompt, verbatim. |
| `should_trigger` | boolean | no | Whether the skill should auto-trigger for it. |
| `id` | string | optional | Stable identifier. A missing `id` is assigned on load (`q1`, `q2`, ... in file order, skipping ids already taken), so two entries with identical `query` text stay distinct. Results and train/test splits are keyed by `id`, never by query text. |

---

## Trigger-optimization results.json

Result of `run_loop.py`. Located at `<skill-name>-workspace/trigger-opt/<timestamp>/results.json` (or `--results-dir`). `run_eval.py` emits the `per_query` and `summary` shape for a single description (no `iterations`). Writer: `run_loop.py` / `run_eval.py`; `generate_report.py` reads it to build `report.html`.

```json
{
  "skill_name": "pdf",
  "cli_version": "2.1.300",
  "started_at": "2026-10-14T10:30:00Z",
  "exit_reason": "all train queries passed",
  "original_description": "...",
  "best_description": "...",
  "best_selection": {"method": "highest held-out test score among tested candidates", "iteration": 2, "train_score": "10/12", "test_score": "6/8"},
  "listing_settings": {
    "SLASH_COMMAND_TOOL_CHAR_BUDGET": null,
    "skillListingBudgetFraction": null,
    "skillListingMaxDescChars": null,
    "skillOverrides": null,
    "trial_listing": "default: --setting-sources project, ~20 skills listed; may be kinder than a crowded real session",
    "description_chars": 412,
    "when_to_use_chars": 0
  },
  "listing_warnings": [],
  "settings": {"runs_per_query": 3, "fixed_runs": false, "max_iterations": 3, "holdout": 0.4, "threshold": 0.5, "model": "claude-sonnet-4-5"},
  "calls": {"planned_worst_case": 180, "made": 112, "canary": 1, "rewrites": 2},
  "total_cost_usd": 3.42,
  "error_counts": {"error_runs": 2, "error_queries": 0},
  "iterations": [
    {
      "iteration": 1,
      "description": "...",
      "train": {"passed": 10, "failed": 2, "total": 12, "per_query": []},
      "test": {"passed": 6, "failed": 2, "total": 8, "per_query": []},
      "calls": [
        {"kind": "canary|trigger|rewrite", "argv": ["claude", "-p", "..."], "extra_args": [], "exit_code": 0, "total_cost_usd": 0.019}
      ]
    },
    {
      "iteration": 2,
      "description": "...",
      "train": {"passed": 9, "failed": 3, "total": 12, "per_query": []},
      "test": "skipped (train regressed)",
      "calls": []
    }
  ]
}
```

**Top level**

| Field | Type | Null | Meaning |
|---|---|---|---|
| `skill_name`, `original_description` | string | no | |
| `cli_version` | string | no | `init.claude_code_version` from the canary; `claude --version` if the canary aborted; `"unknown"` when neither is available. |
| `started_at` | string | no | |
| `exit_reason` | string | no | e.g. `all train queries passed`, `max_iterations (3)`, `max_total_usd reached`, `canary failed`. |
| `best_description` | string | yes | The selected description; `null` if nothing was tested (canary failed). |
| `best_selection` | object | yes | How `best_description` was chosen: `method` (`highest held-out test score among tested candidates`, or `highest train score (no held-out set: --holdout 0)` when there is no test set; `test_score` is then `null`), `iteration`, `train_score` and `test_score` as `"passed/total"` strings. A candidate whose `test` is not an object (skipped or not run), or that is `incomplete`, is never selected. |
| `listing_settings` | object | no | The effective listing settings, each `null` when unset: `SLASH_COMMAND_TOOL_CHAR_BUDGET` (env), `skillListingBudgetFraction`, `skillListingMaxDescChars`, `skillOverrides` (the entry for this skill), plus `trial_listing` (the note that trials use the default listing), `description_chars`, `when_to_use_chars`. |
| `listing_warnings` | string[] | no | Warnings that `description` + `when_to_use` exceeds the listing cut while `should_trigger` train queries fail, so under-triggering may be truncation. Each distinct warning once. |
| `canary_error` | string | optional | Present only when the canary failed (`exit_reason` is `canary failed`): why. |
| `settings` | object | no | Effective run settings: `runs_per_query` (the maximum), `fixed_runs`, `max_iterations`, `holdout`, `threshold`, `model`. |
| `calls` | object | no | `planned_worst_case` (queries x max runs x iterations + canary + rewrites), `made`, `canary`, `rewrites`. |
| `total_cost_usd` | number | no | Cumulative `result.total_cost_usd` over every call. `--max-total-usd` compares against this. |
| `error_counts` | object | no | `error_runs` (runs with outcome `error`) and `error_queries` (queries reported `error`). |
| `iterations[]` | array | no | Iteration 1 always tests the original description. |

**`iterations[]`**

| Field | Type | Null | Meaning |
|---|---|---|---|
| `iteration` | integer | no | 1-based. |
| `description` | string | no | The description tested. |
| `train` | object | no | `passed`, `failed`, `total`, `per_query[]`. `error` queries are counted in `total` but in neither `passed` nor `failed`; use `errors`. |
| `train.errors` | integer | no | Queries reported `error`. |
| `test` | object \| string | yes | Same shape as `train`; the string `"skipped (train regressed)"` when this candidate's train score was below the best so far; `"not run (max_total_usd reached)"` when the cost cap stopped the iteration first; `null` when there is no held-out set. |
| `incomplete` | boolean | optional | `true` when the cost cap stopped this iteration part-way. Its scores cover only the runs made, and it is never selected as best. Absent otherwise. |
| `calls[]` | array | no | One entry per CLI call: `kind` (`canary`, `trigger`, `rewrite`), `argv`, `extra_args`, `exit_code`, `total_cost_usd` (nullable). |

**`per_query[]`** (same shape in `run_eval.py` output `results[]`)

| Field | Type | Null | Meaning |
|---|---|---|---|
| `id` | string | no | From the eval set. |
| `query` | string | no | |
| `should_trigger` | boolean | no | |
| `outcomes[]` | array | no | One per run, in order: `{"outcome": "triggered" \| "not_triggered" \| "error", "exit_code": integer or null, "stderr_tail": string or null, "reason": string}`. `exit_code` and `stderr_tail` are set for `error` and may be set otherwise. `reason` is present only on `error` (e.g. `timeout`, `exit code 1`, `permission denied: Bash`). |
| `runs_used` | integer | no | `len(outcomes)`: 2 under adaptive runs unless the first two non-error outcomes disagreed (then 3); `--runs-per-query` under `--fixed-runs`. |
| `trigger_rate` | number | yes | `triggered / (triggered + not_triggered)`; `null` when no non-error outcome. |
| `result` | `"pass"` \| `"fail"` \| `"error"` | no | `error` when fewer than two non-error outcomes remain. Otherwise `pass` when `trigger_rate >= threshold` for `should_trigger: true`, or `< threshold` for `false`. |

Removed: `history.json`; the old `history`, `train_results`, and `test_results` arrays; results keyed by query text.

---

## comparison.json

Output from blind comparator. Located at `<grading-dir>/comparison-N.json`.

```json
{
  "winner": "A",
  "reasoning": "Output A provides a complete solution with proper formatting and all required fields. Output B is missing the date field and has formatting inconsistencies.",
  "rubric": {
    "A": {
      "content": {
        "correctness": 5,
        "completeness": 5,
        "accuracy": 4
      },
      "structure": {
        "organization": 4,
        "formatting": 5,
        "usability": 4
      },
      "content_score": 4.7,
      "structure_score": 4.3,
      "overall_score": 9.0
    },
    "B": {
      "content": {
        "correctness": 3,
        "completeness": 2,
        "accuracy": 3
      },
      "structure": {
        "organization": 3,
        "formatting": 2,
        "usability": 3
      },
      "content_score": 2.7,
      "structure_score": 2.7,
      "overall_score": 5.4
    }
  },
  "output_quality": {
    "A": {
      "score": 9,
      "strengths": ["Complete solution", "Well-formatted", "All fields present"],
      "weaknesses": ["Minor style inconsistency in header"]
    },
    "B": {
      "score": 5,
      "strengths": ["Readable output", "Correct basic structure"],
      "weaknesses": ["Missing date field", "Formatting inconsistencies", "Partial data extraction"]
    }
  },
  "expectation_results": {
    "A": {
      "passed": 4,
      "total": 5,
      "pass_rate": 0.80,
      "details": [
        {"text": "Output includes name", "passed": true}
      ]
    },
    "B": {
      "passed": 3,
      "total": 5,
      "pass_rate": 0.60,
      "details": [
        {"text": "Output includes name", "passed": true}
      ]
    }
  }
}
```

---

## analysis.json

Output from post-hoc analyzer. Located at `<grading-dir>/analysis.json`.

```json
{
  "comparison_summary": {
    "winner": "A",
    "winner_skill": "path/to/winner/skill",
    "loser_skill": "path/to/loser/skill",
    "comparator_reasoning": "Brief summary of why comparator chose winner"
  },
  "winner_strengths": [
    "Clear step-by-step instructions for handling multi-page documents",
    "Included validation script that caught formatting errors"
  ],
  "loser_weaknesses": [
    "Vague instruction 'process the document appropriately' led to inconsistent behavior",
    "No script for validation, agent had to improvise"
  ],
  "instruction_following": {
    "winner": {
      "score": 9,
      "issues": ["Minor: skipped optional logging step"]
    },
    "loser": {
      "score": 6,
      "issues": [
        "Did not use the skill's formatting template",
        "Invented own approach instead of following step 3"
      ]
    }
  },
  "improvement_suggestions": [
    {
      "priority": "high",
      "category": "instructions",
      "suggestion": "Replace 'process the document appropriately' with explicit steps",
      "expected_impact": "Would eliminate ambiguity that caused inconsistent behavior"
    }
  ],
  "transcript_insights": {
    "winner_execution_pattern": "Read skill -> Followed 5-step process -> Used validation script",
    "loser_execution_pattern": "Read skill -> Unclear on approach -> Tried 3 different methods"
  }
}
```

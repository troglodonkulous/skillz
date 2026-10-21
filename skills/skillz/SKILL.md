---
name: skillz
description: Create, edit, evaluate, and improve Claude Code skills. Use this skill whenever the user wants to make a new skill, turn a workflow or conversation into a skill, update or fix an existing skill, test whether a skill works, compare two versions, or fix a skill that triggers too rarely or too often. Also use it for any mention of SKILL.md, skill descriptions, skill frontmatter, skill evals, or packaging a skill, even if the user never says "skill creator".
---

# skillz

A skill is an onboarding guide for another Claude instance: it carries procedure and context the model lacks. Write for that reader.

## Principles

- **Context is a shared budget.** The skill competes with the conversation and every other skill. Claude is already smart, so add only what it cannot infer, and cut any paragraph that does not earn its tokens.
- **Match freedom to fragility.** Open-ended judgment calls get plain-language guidance (high freedom). A preferred pattern gets pseudocode or a parameterized script (medium). Fragile, order-sensitive operations get one exact script with few options (low). A narrow bridge needs guardrails; an open field does not.
- **Explain why.** A model that knows the reason generalizes; a model given an unexplained rule follows it rigidly or ignores it. A rule shouted in capitals is a yellow flag: reframe it as a reason. Hard rules are fine when the reason travels with them.
- **Generalize, don't overfit.** A skill is used on many prompts you have not seen. Fix the cause behind a failure, not the one example that exposed it.
- **Match the user's vocabulary.** "Evaluation" and "benchmark" are fine. Explain "JSON" or "expectation" unless the user's own words show they know them.

## Mode gate

Pick the mode from the request. State which one you chose and why. Modes compose (Author, then Evaluate, then Optimize Triggers is the full lifecycle), but none implies another.

| Mode | Use when | Cost | Load |
|---|---|---|---|
| **Author** (default) | New skill, edit to an existing one, or the user wants a result now | Minutes | This file, plus pattern references as needed |
| **Evaluate** | Outputs are objectively checkable, the skill will be reused or shared, a regression is suspected, or the user asks to test | Tens of `claude -p` runs | `references/evaluation.md`, `references/schemas.md`, `agents/grader.md` |
| **Compare** | "Is v2 actually better?" | Evaluate plus two judge passes | The Evaluate set, plus `agents/comparator.md` and `agents/analyzer.md` |
| **Optimize Triggers** | The skill works but fires too rarely or too often. Not for `disable-model-invocation` skills | About 100-180 `claude -p` calls on the session model; needs the `claude` CLI | `references/trigger-optimization.md` |

If the user already has a draft, skip to the mode they need. If they say "just vibe with me", stay in Author and skip measurement.

## Guardrails

Each holds in every mode.

1. **Mine before asking.** If the conversation already shows the workflow, reconstruct it first. Interrogating the user about something they just did wastes their time.
2. **Disclose cost before spending it.** Any step over about 10 model invocations states its budget and waits for a yes.
3. **Skip checks for subjective skills.** Writing tone, visual style, and prose have no pass/fail, so formal checks would measure the wrong thing. Say that plainly, recommend qualitative review instead, and let the user decide.
4. **Keep workspaces outside every skills directory.** Eval workspaces, snapshots, and results never go in `~/.claude/skills/`, `.claude/skills/`, or a plugin's `skills/`. Anything there is loaded as a live skill.
5. **Never write under `~/.claude/plugins/`.** It is the plugin cache and updates overwrite it. For a plugin skill, edit the plugin's source repo, or copy the skill to personal or project scope and tell the user the copy now shadows or duplicates it.
6. **Preserve identity when updating.** Keep the directory name and `name` unchanged, so existing invocations and references keep working.
7. **Don't touch the user's other skills, commands, or settings** while evaluating. Installing the skill you are authoring is the one explicit write, and the user chose it.
8. **Never write a package into its own source tree.** The archive would contain itself on the next run.
9. **Don't free a port by killing the process holding it.** It may belong to something the user cares about. Bind port 0 instead.
10. **Show the human the results before you judge them.** In Evaluate and Compare, generate the review viewer before stating your own assessment.
11. **Lack of surprise.** No malware, exploit code, or exfiltration helpers, and no skill whose behavior would surprise a user who read its description. Roleplay and persona skills are fine.

## Author path

Follow these in order, skipping a step only when it clearly does not apply. The path ends at an installed, validated, smoke-checked skill. It does not load evaluation references, and it does not package.

### 1. Capture intent from context

Read the conversation first: the tools used, the sequence of steps, the corrections the user made, the input and output formats. Present the reconstruction and ask for confirmation, then ask a few questions for what is still missing. Start with the most important, because a wall of questions stalls the user.

### 2. Get concrete examples

Ask what the user would say to trigger the skill and what output they expect. Stop when the functionality is clear.

### 3. Choose the mode

Recommend Evaluate when outputs are objectively checkable (file transforms, data extraction, code generation, fixed workflow steps). For subjective skills, apply guardrail 3 and give the reason. The user decides.

### 4. Plan the reusable contents

For each example, ask how it would be done from scratch and what would be re-derived every time. That list, and only that list, becomes the bundled resources:

- `scripts/`: code that is rewritten repeatedly or must be deterministic. Support `--help` so the agent learns options without reading source. See `references/script-patterns.md`.
- `references/`: material loaded only when needed (schemas, API docs, policies, long guides). Link each from SKILL.md and say when to read it. Put a table of contents on any file over 100 lines. Keep files one hop from SKILL.md, and keep each fact in one place.
- `assets/`: files used in the output rather than read (templates, icons, fonts, boilerplate).

Don't ship `README.md`, `CHANGELOG.md`, `INSTALLATION_GUIDE.md`, or any other auxiliary document: the reader is an agent, and notes about how the skill was made are clutter. Don't add a script, option, or reference that no concrete example demands. Missing resources are cheap to add during iteration; unused ones cost context on every load.

For multi-step or branching processes see `references/workflows.md`. For fixed output formats see `references/output-patterns.md`. For skills that wrap a service API see `references/api-skills.md`.

### 5. Choose the install location and initialize

Ask once: personal (`~/.claude/skills/`) or project (`.claude/skills/`). If the user is working in a plugin's source repo, that plugin's own `skills/` directory is a third option. Then scaffold. The name is validated before anything is written, and only the directories named in `--with` are created:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/init_skill.py <skill-name> --path <install-dir> --with <dirs from step 4>
```

`<dirs from step 4>` is the comma list of only the resource directories step 4 produced (any of `scripts`, `references`, `assets`). Omit `--with` for a SKILL.md-only skill. To update an existing skill, skip this step.

### 6. Implement resources first, then SKILL.md

Write and run every bundled script at least once before documenting it. A script that was never run is a guess, and a copied variant can still have a bad option or path. Reference scripts from the generated SKILL.md as `${CLAUDE_SKILL_DIR}/scripts/<name>.py`, because a bare relative path breaks when the agent works from another directory. Delete anything the examples did not demand.

Write the body in imperative form, for another Claude: non-obvious procedure, domain facts, and pointers. Keep it lean, put essential rules first, and give the reason for each rule.

### 7. Write the frontmatter

`name` and `description` are always generated. The description is the main trigger text Claude sees (`when_to_use` can add more, see `references/claude-code.md`), so it carries both what the skill does and when to use it, including contexts the user might not name. Claude tends to under-trigger, so lean slightly pushy. Compare:

- Weak: `Build dashboards for internal data.`
- Pushy: `Build dashboards for internal data. Use whenever the user mentions dashboards, metrics, data visualization, or wants to display company data, even if they never say "dashboard".`

Keep it under 1,024 characters with no angle brackets. Put no "When to use this skill" section in the body: the body loads only after the skill has already triggered.

Add Claude Code fields where they earn their place; see `references/claude-code.md`. A skill with side effects (deploy, send, delete, publish) gets `disable-model-invocation: true`, unless the user explicitly wants it to trigger from plain language; then keep auto-invocation and tell them the risk. Note when a field makes the skill non-portable (`references/environments.md`).

### 8. Validate

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/quick_validate.py <skill-dir>
```

The default target is `claude-code`. Errors block; fix them. Warnings are reported, so read each and decide whether it is real.

### 9. Smoke-check the install

Valid files do not prove a working install. Run the checks in `references/claude-code.md` (listed in a fresh session, not shadowed, invokes correctly with its arguments and resources). Then give the user the skill's path and the command to run it.

### 10. Package only to share

Packaging is for sharing or uploading, not for using the skill. When the user wants a file to send:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/package_skill.py <skill-dir> --output <dir-outside-the-skill>
```

It validates against the portable target first and writes a `.skill` zip. Porting caveats are in `references/environments.md`.

### 11. Iterate from real use

Use the skill on real tasks and notice where the model struggles. Read what it actually did, not only the final output: wasted steps point at instructions to cut. When several runs each wrote the same helper script, bundle that script. Make the fix general rather than patching the one example, keep the prompt lean, and explain the reason behind each change.

## Pointers

- Evaluate, Compare: `references/evaluation.md`
- Optimize Triggers: `references/trigger-optimization.md`
- Data formats: `references/schemas.md`

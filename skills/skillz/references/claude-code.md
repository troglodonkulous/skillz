# Claude Code Features for Generated Skills

Claude Code is the primary target. Use these features where they earn their place, and note when one makes a skill non-portable (claude.ai upload, the Skills API, and packaging reject keys outside the six spec fields; see `environments.md`).

## Contents

- [Frontmatter fields](#frontmatter-fields)
- [Body features](#body-features)
- [Where skills live](#where-skills-live)
- [Install checks](#install-checks)

## Frontmatter fields

Every field is optional in Claude Code. `name` falls back to the directory name and `description` to the first body line, but write both: a missing description hurts triggering. Unknown keys are ignored silently.

| Field | Use it when | Why |
|---|---|---|
| `disable-model-invocation: true` | The skill has side effects: deploy, send, delete, publish | Only the user can start it, so Claude never fires it on a hunch. Also keeps it out of trigger optimization. If the user explicitly asks for plain-language triggering, honor that and say Claude may then start it unprompted. |
| `allowed-tools` | The skill runs the same few tools every time | Skips a permission prompt per call. Keep the list to what the skill needs. |
| `context: fork` with `agent` | Heavy work that would fill the main context | Runs in a subagent with its own context. A forked skill has no conversation history, so never fork a skill that needs it. |
| `paths` | The skill only matters for certain files (globs) | Claude loads it when matching files are in play. |
| `argument-hint`, `arguments` | The user invokes it as `/name <args>` | Shows the expected arguments and names them for substitution. |
| `when_to_use` | The trigger context does not fit the description | Extra trigger text. `description` + `when_to_use` are capped at 1,536 characters combined in the skill listing. |

`name` is 1-64 chars of lowercase letters, digits, and hyphens. Keep it equal to the directory name so the skill stays portable.

## Body features

- `${CLAUDE_SKILL_DIR}`: the skill's own directory. Reference every bundled script through it, for example `uv run ${CLAUDE_SKILL_DIR}/scripts/rotate.py --help`. A bare relative path breaks when the agent's working directory is elsewhere.
- `$ARGUMENTS`, plus named substitutions from `arguments`: the text the user typed after the slash command.
- Dynamic injection (a shell command whose output is spliced into the body before Claude sees it): use only when the skill needs live data at load time. It runs shell commands, so it needs permission and a plan for failure. Injected output also counts toward the rendered size.

Example of a side-effect skill header:

```yaml
---
name: deploy-staging
description: Deploy the current branch to staging and verify the health check. Use when the user says deploy, ship to staging, or push to staging.
disable-model-invocation: true
allowed-tools: Bash(git *) Bash(uv run *)
---
```

## Where skills live

| Scope | Directory | Pick when |
|---|---|---|
| Personal | `~/.claude/skills/<name>/` | The skill follows the user across projects |
| Project | `.claude/skills/<name>/` | The team should get it through the repo |
| Plugin | the plugin source repo's `skills/` | The user is working in a plugin's source |

Plugin skills are edited in the plugin's source repo, never in the plugin cache (see the guardrail in `SKILL.md`).

## Install checks

Valid files do not prove a working install.

- Confirm the intended copy is listed in a fresh session.
- Look for a same-name skill in another scope that would shadow it. Plugin skills use the qualified `plugin:skill` command.
- Edits to an existing skill reload without a restart. A newly created top-level skills directory needs `/reload-skills`.
- After compaction, Claude Code keeps only the first 5,000 tokens of each invoked skill (25,000 combined), so essentials go first in the body.

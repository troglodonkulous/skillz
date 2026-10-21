# Porting and Degradation

skillz targets Claude Code. Read this only when a skill must run elsewhere, or when a mode cannot run as written.

## Contents

- [Porting to claude.ai or the Skills API](#porting-to-claudeai-or-the-skills-api)
- [When a mode cannot run](#when-a-mode-cannot-run)

## Porting to claude.ai or the Skills API

These surfaces hard-fail on any frontmatter key outside the six spec fields (`name`, `description`, `license`, `allowed-tools`, `metadata`, `compatibility`). Before packaging, run the portable check:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/quick_validate.py <skill-dir> --target portable
```

Then drop or change:

- Claude Code-only keys: `disable-model-invocation`, `context`, `agent`, `paths`, `argument-hint`, `arguments`, `when_to_use`. The skill still works without them, but the behavior they gave is lost. For a side-effect skill, say so in the body and have the skill ask before acting.
- Claude Code-only body features: `${CLAUDE_SKILL_DIR}`, `$ARGUMENTS`, and dynamic injection. Use paths relative to the skill root instead.
- Assumed tools and network. The API sandbox has no network and no package installs, so scripts must not depend on either. Check what the target surface provides.

Both `name` and `description` are required on these surfaces. `name` must match the directory.

## When a mode cannot run

| Missing | Effect |
|---|---|
| Subagents | Evaluate and Compare cannot isolate a baseline from the candidate skill, and a baseline that can see the candidate is contaminated. Report no deltas; review the outputs qualitatively and say why. |
| The `claude` CLI | Optimize Triggers cannot run, because it measures real triggering with `claude -p`. Skip it and tighten the description by hand using the pushy what+when rule. |
| A display or browser | Generate the review as a static HTML file and hand the user the path instead of opening a viewer. |

Author mode needs none of these.

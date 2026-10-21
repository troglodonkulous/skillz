# skillz

A skill for making Claude Code skills: write them well, test that they work, and tune when they fire.

## How it started

I built my own skill-creator skill, based on the methodology in [this video](https://www.youtube.com/watch?v=OIKTsVjTVJE) by IndyDevDan ([disler](https://github.com/disler)). Only afterward did I learn Anthropic has an official skill-creator. I never used it once.

Months later I compared the two, then frankensteined the best parts of each into one monstrous giga-skill that can only be described as… skillz.

## What it is

skillz takes the authoring craft from my disler-inspired skill (how to write a skill another Claude can actually use, plus scripts to scaffold, validate, and package it) and adds Anthropic's measurement tools: test runs against a baseline, grading, side-by-side comparison, and trigger tuning, with the bugs in that tooling fixed.

The two halves overlap less than the shared name suggests, and most skills only need the first.

## Four modes

skillz picks a mode from your request and tells you which one and why. Only that mode's material loads, so a quick "make me a skill" stays quick.

- **Author** (default): turns a workflow, often the one you just did in the conversation, into an installed skill. It reconstructs what you did before asking questions, bundles only the scripts and references your examples need, writes a description that triggers reliably, then validates and smoke-checks the install. Takes minutes.
- **Evaluate**: runs realistic test prompts with and without the skill, grades the outputs, and opens a review page so you see the results before Claude judges them. Skipped for subjective skills (tone, style), where pass/fail measures the wrong thing. Tens of runs.
- **Compare**: is v2 actually better? Evaluates both versions, then a blind judge picks a winner and explains why.
- **Optimize Triggers**: for a skill that works but fires too rarely or too often. Tests the description against should- and shouldn't-trigger queries, rewrites it, and keeps the version that scores best on queries held out from tuning. About 100–180 calls.

## Why use it

- **Lean by design.** A skill shares context with everything else, so skillz teaches cutting what Claude already knows and matching guardrails to how fragile each step is. Its own `SKILL.md` is 132 lines.
- **Built for Claude Code.** Validates against Claude Code's frontmatter, confirms the installed skill actually shows up, and keeps skills with side effects (deploy, send, delete) from firing on their own. `.skill` packaging for claude.ai is still there for sharing.
- **Honest about cost.** Anything over about 10 model calls states its budget and waits for a yes. `--max-total-usd` caps spend.
- **Measures the real thing.** Each test run is a fresh, isolated session that loads the skill the way Claude Code does. Baseline runs that could see the skill are thrown out, and errors count as errors, not as "didn't trigger".
- **Safe for your setup.** Never writes inside your skills or plugin folders, never touches your other skills, never kills a process to free a port, and cleans up temp files even after a crash.

## Install

Requires [uv](https://docs.astral.sh/uv/). Evaluate, Compare, and Optimize Triggers also need the `claude` CLI.

**Claude Code**

```
/plugin marketplace add troglodonkulous/skillz
/plugin install skillz@skillz
```

Then turn on auto-update under `/plugin` > Marketplaces so pushes reach you. Start a new session and ask Claude to make a skill, or run `/skillz:skillz`. If another skill-creator is installed, remove it so the two don't compete for the same requests.

**claude.ai and Claude Desktop**: Customize > Plugins > Add > Add marketplace > Add from a repository, then enter `troglodonkulous/skillz`. Skills work in chat; the eval runner needs the Claude Code CLI.

After a Claude Code upgrade, run the offline tests to confirm nothing in the CLI's output format broke:

```bash
uv run --with pytest --with pyyaml --with click pytest skills/skillz/tests -q
```

## Roadmap

skillz is built tightly around Claude Code today. Next is the same deep, harness-level integration for other agents, starting with Codex and Hermes.

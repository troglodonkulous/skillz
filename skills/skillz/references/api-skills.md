# Skills That Wrap an API

Apply these patterns when the skill's scripts call an external service. Script conventions (`uv run`, `--json`, self-contained files, tiered output) are in `script-patterns.md`.

## Contents

- [Instruction directives](#instruction-directives)
- [Filename and date conventions](#filename-and-date-conventions)
- [Document each script](#document-each-script)

## Instruction directives

`--json`, `--help`, and API-key prerequisites follow `script-patterns.md`. Add these API-specific directives to the generated body, each with its reason:

- State the rate limit (X requests/minute) so the agent paces calls instead of discovering the limit through errors.
- Name one timezone for every timestamp, so saved files sort and compare consistently.

## Filename and date conventions

When scripts save files, fix one pattern and say why (sortable, greppable):

```
YYYY-MM-DD-HHmm_skillname_short-descriptive-title.ext
```

Offer `-o` to override the name when the user asks for a specific one.

When saved notes carry frontmatter, fix its shape too:

```yaml
created_date: [[YYYY-MM-DD]]
created_time: "HH:mm"  # omit if the time is unknown
```

## Document each script

Give every script a short entry that says when to reach for it:

```markdown
### `scripts/list_items.py`
**When to use:** list items, search by keyword, filter by date
- Search: `--search "query"`
- Pagination: `--cursor CURSOR`
- Metadata only by default; add `--include-details` for more
```

For data-heavy APIs, use the tiered approach in `script-patterns.md` (metadata, then details, then full content).

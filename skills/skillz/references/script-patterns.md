# Script Patterns

Patterns for building effective, context-efficient scripts in skills.

## Contents

- [Script Execution with `uv run`](#script-execution-with-uv-run)
- [CLI Conventions](#cli-conventions)
- [Self-Contained Architecture](#self-contained-architecture)
- [Data-Level Progressive Disclosure](#data-level-progressive-disclosure)
- [Save-Without-Reading Pattern](#save-without-reading-pattern)
- [Destructive Operation Patterns](#destructive-operation-patterns)
- [Error Handling](#error-handling)
- [Environment Variables for API Keys](#environment-variables-for-api-keys)

## Script Execution with `uv run`

Use inline dependency declarations so scripts are self-contained and executable without separate requirements files:

```python
#!/usr/bin/env python3
# /// script
# dependencies = [
#     "httpx",
#     "click",
# ]
# ///
```

Run with: `uv run ${CLAUDE_SKILL_DIR}/scripts/script.py [options]`, so it works from any working directory

This pattern:
- Declares dependencies inline (no requirements.txt needed)
- Auto-installs dependencies on first run
- Makes scripts portable and self-documenting

## CLI Conventions

### Always Support `--help`

Use `click` for CLI parsing. This enables discoverability without reading source:

```python
@click.command()
@click.option("--limit", "-l", default=10, help="Max items to return")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def main(limit: int, output_json: bool):
    """Brief description of what this script does."""
    ...
```

**SKILL.md should instruct:**
> Don't read scripts unless absolutely needed — run `uv run ${CLAUDE_SKILL_DIR}/scripts/<script>.py --help` to understand options

### Always Support `--json`

Every script that outputs data should support `--json` for machine-readable output:

```python
if output_json:
    click.echo(json.dumps(data, indent=2))
else:
    click.echo(format_human_readable(data))
```

**SKILL.md should instruct:**
> Default to `--json` flag for all commands when processing data

## Self-Contained Architecture

Each script should embed all necessary code — no shared modules between scripts.

**Why:** Scripts may be executed without loading into context. If Script A imports from Script B, Claude must read both to understand either.

**Exception:** skillz's own toolchain shares `validation.py` across its scripts on purpose, because two copies of a correctness boundary drift apart. That is a maintainer trade-off. Skills you generate should keep each script self-contained.

**Pattern:**
```
scripts/
├── list_items.py      # Has its own HTTP client
├── get_item.py        # Has its own HTTP client (duplicated)
├── delete_item.py     # Has its own HTTP client (duplicated)
```

**Trade-off:** Code duplication for isolation. This is intentional — each script is portable and context-efficient.

**Typical structure (~200-300 lines):**
```python
#!/usr/bin/env python3
# /// script
# dependencies = ["httpx", "click"]
# ///

"""Script description and usage examples."""

import json, os, sys
import click
import httpx

# Configuration
API_BASE_URL = "https://api.example.com"
API_TIMEOUT = 30.0

class APIClient:
    """Embedded HTTP client for this script only."""
    def __init__(self, api_key: str):
        self.client = httpx.Client(
            base_url=API_BASE_URL,
            timeout=API_TIMEOUT,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    # ... methods ...

def format_output(data):
    """Human-readable formatting."""
    ...

@click.command()
@click.option("--json", "output_json", is_flag=True)
def main(output_json: bool):
    """Main entry point."""
    ...

if __name__ == "__main__":
    main()
```

## Data-Level Progressive Disclosure

For APIs that return large responses, default to minimal data and offer opt-in flags for more:

```python
@click.option("--include-details", is_flag=True, help="Include detailed content")
@click.option("--include-full", is_flag=True, help="Include complete response")
```

**SKILL.md should document tiers:**
```markdown
### Tier 1: Metadata Only (Default)
Returns: id, title, timestamp. Minimal tokens.

### Tier 2: Add Details
Use `--include-details` for structured content.

### Tier 3: Full Content
Use `--include-full` for complete data. Only when needed.
```

## Save-Without-Reading Pattern

For exporting data without consuming context, create dedicated save scripts:

```python
@click.command()
@click.argument("item_id")
@click.option("--output", "-o", help="Output file path")
def main(item_id: str, output: str):
    """Save item to file WITHOUT printing content to stdout."""
    data = fetch_item(item_id)
    
    # Write to file
    Path(output).write_text(generate_content(data))
    
    # Only print confirmation, not content!
    click.echo(f"✅ Saved: {output}")
    click.echo(f"   Size: {len(content):,} characters")
```

**SKILL.md should document:**
> To export without reading into context: `uv run ${CLAUDE_SKILL_DIR}/scripts/save_item.py ITEM_ID`

## Destructive Operation Patterns

### Confirmation Required
```python
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def main(item_id: str, yes: bool):
    if not yes:
        click.echo("⚠️ This will permanently delete the item.")
        if not click.confirm("Continue?"):
            sys.exit(0)
    # proceed with deletion
```

### Dry-Run Support
```python
@click.option("--dry-run", is_flag=True, help="Show what would happen without doing it")
def main(dry_run: bool):
    if dry_run:
        click.echo(json.dumps({"would_delete": item_id, "valid": True}))
        return
    # proceed with actual operation
```

## Error Handling

Return structured errors in JSON mode:

```python
try:
    result = do_operation()
except Exception as e:
    if output_json:
        click.echo(json.dumps({"error": str(e)}))
    else:
        click.echo(f"❌ Error: {e}", err=True)
    sys.exit(1)
```

## Environment Variables for API Keys

```python
api_key = os.environ.get("SERVICE_API_KEY")
if not api_key:
    error = "SERVICE_API_KEY environment variable not set"
    if output_json:
        click.echo(json.dumps({"error": error}))
    else:
        click.echo(f"❌ Error: {error}", err=True)
    sys.exit(1)
```

**SKILL.md prerequisites:**
```markdown
## Prerequisites
- **API Key Required**: Set `SERVICE_API_KEY` environment variable
```


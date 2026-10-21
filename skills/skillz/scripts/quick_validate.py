#!/usr/bin/env python3
# /// script
# dependencies = [
#     "click",
#     "pyyaml",
# ]
# ///

"""
Skill Validator - validate a skill directory against the Agent Skills format.

All rules live in validation.py (section 4.3 of the spec). This is the CLI.

Usage:
    uv run quick_validate.py <skill-directory> [--target claude-code|portable] [--json] [--strict]
"""

import json
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validation  # noqa: E402


def validate_skill(skill_path, target="claude-code"):
    """Return (is_valid, message, details). Details carry checks[], errors, warnings, notices."""
    result = validation.validate_skill(skill_path, target)
    is_valid, message, details = result.as_tuple()
    details.update(errors=result.errors, warnings=result.warnings, notices=result.notices)
    return is_valid, message, details


@click.command()
@click.argument("skill_directory", type=click.Path(exists=True, file_okay=False, dir_okay=True))
@click.option("--target", type=click.Choice(validation.TARGETS), default="claude-code", show_default=True,
              help="claude-code accepts Claude Code's own frontmatter keys; portable allows only the six spec keys.")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.option("--strict", is_flag=True, help="Promote warnings to errors (notices are never promoted)")
def main(skill_directory: str, target: str, output_json: bool, strict: bool):
    """
    Validate a skill directory structure and SKILL.md contents.

    SKILL_DIRECTORY is the path to the skill folder to validate. Exit 1 on error,
    0 otherwise (warnings are printed).
    """
    try:
        result = validation.validate_skill(skill_directory, target)
        if strict:
            validation.apply_strict(result)
    except Exception as e:
        if output_json:
            click.echo(json.dumps({"valid": False, "error": str(e)}))
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if output_json:
        click.echo(json.dumps(result.to_dict(), indent=2))
    else:
        if result.is_valid:
            click.echo(f"OK: {result.message}")
            info = result.details
            click.echo(f"   Name: {info.get('name', 'N/A')}   Target: {target}")
        else:
            click.echo("INVALID:", err=True)
        for e in result.errors:
            click.echo(f"  error [{e['check']}]: {e['message']}", err=True)
        for w in result.warnings:
            click.echo(f"  warning [{w['check']}]: {w['message']}")
        for n in result.notices:
            click.echo(f"  notice [{n['check']}]: {n['message']}")
    sys.exit(0 if result.is_valid else 1)


if __name__ == "__main__":
    main()

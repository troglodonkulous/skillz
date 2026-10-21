#!/usr/bin/env python3
# /// script
# dependencies = [
#     "click",
#     "pyyaml",
# ]
# ///

"""
Skill Initializer - Creates a new skill from template

Creates a skill directory containing SKILL.md (frontmatter plus TODO placeholders).
Resource directories are opt-in: --with scripts,references,assets creates them empty.
The name is validated before anything is created.

Usage:
    uv run init_skill.py <skill-name> --path <output-directory> [--with scripts,references,assets]
    uv run init_skill.py my-skill --path ./skills --json
"""

import json
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validation  # noqa: E402


# =============================================================================
# Templates
# =============================================================================

SKILL_TEMPLATE = """---
name: {skill_name}
description: "TODO: State what the skill does and when to use it: the tasks, file types, or phrases that should trigger it."
---

# {skill_title}

[TODO: One or two sentences on what this skill enables.]

## [TODO: First main section]

[TODO: Instructions Claude needs and would not infer on its own. Reference every
bundled file from here so Claude knows it exists. Delete sections that do not earn
their place.]
"""

RESOURCE_CHOICES = ("scripts", "references", "assets")


def title_case_skill_name(skill_name: str) -> str:
    """Convert hyphenated skill name to Title Case for display."""
    return " ".join(word.capitalize() for word in skill_name.split("-"))


def init_skill(skill_name: str, path: str, resources: tuple[str, ...] = ()) -> tuple[bool, str, dict]:
    """
    Create a new skill directory containing SKILL.md plus any requested empty resource dirs.

    Args:
        skill_name: Name of the skill (hyphen-case, at most 64 chars)
        path: Directory in which the skill directory is created
        resources: Subset of scripts, references, assets to create as empty directories

    Returns:
        Tuple of (success, message, details_dict)
    """
    details = {"skill_name": skill_name, "path": path, "created_files": []}

    problem = validation.check_name(skill_name)
    if problem:
        return False, problem, details
    unknown = [r for r in resources if r not in RESOURCE_CHOICES]
    if unknown:
        return False, f"Unknown --with value(s): {', '.join(unknown)}. Choose from {', '.join(RESOURCE_CHOICES)}", details

    skill_dir = Path(path).resolve() / skill_name
    details["skill_dir"] = str(skill_dir)
    if skill_dir.exists():
        return False, f"Skill directory already exists: {skill_dir}", details

    try:
        skill_dir.mkdir(parents=True, exist_ok=False)
        (skill_dir / "SKILL.md").write_text(
            SKILL_TEMPLATE.format(skill_name=skill_name, skill_title=title_case_skill_name(skill_name))
        )
        details["created_files"].append("SKILL.md")
        for name in dict.fromkeys(resources):
            (skill_dir / name).mkdir()
            details["created_files"].append(f"{name}/")
    except OSError as e:
        return False, f"Error creating skill: {e}", details

    return True, f"Skill '{skill_name}' initialized successfully", details


# =============================================================================
# CLI
# =============================================================================

@click.command()
@click.argument("skill_name")
@click.option("--path", "-p", required=True, help="Directory where skill folder will be created")
@click.option("--with", "with_", default="", metavar="LIST",
              help="Comma list of empty resource dirs to create: scripts,references,assets (default: none)")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def main(skill_name: str, path: str, with_: str, output_json: bool):
    """
    Initialize a new skill directory with template files.

    SKILL_NAME should be hyphen-case (e.g., 'my-new-skill').

    \b
    Skill name requirements:
      - Hyphen-case identifier (e.g., 'data-analyzer')
      - Lowercase letters, digits, and hyphens only
      - Max 64 characters
      - Must match directory name exactly
    """
    try:
        success, message, details = init_skill(skill_name, path, tuple(r.strip() for r in with_.split(',') if r.strip()))

        if output_json:
            result = {
                "success": success,
                "message": message,
                "details": details,
            }
            click.echo(json.dumps(result, indent=2))
        else:
            if success:
                click.echo(message)
                click.echo(f"   Location: {details['skill_dir']}")
                click.echo("")
                click.echo("   Created files:")
                for f in details["created_files"]:
                    click.echo(f"     - {f}")
                click.echo("")
                click.echo("   Next steps:")
                click.echo("   1. Edit SKILL.md to complete the TODO items and write the description")
                click.echo("   2. Add only the files the skill needs, and link each from SKILL.md")
                click.echo("   3. Run quick_validate.py on the skill directory")
            else:
                click.echo(f"Error: {message}", err=True)

        sys.exit(0 if success else 1)

    except Exception as e:
        if output_json:
            click.echo(json.dumps({"success": False, "error": str(e)}))
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

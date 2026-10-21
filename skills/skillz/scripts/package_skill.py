#!/usr/bin/env python3
# /// script
# dependencies = [
#     "click",
#     "pyyaml",
# ]
# ///

"""
Skill Packager - creates a distributable .skill file (zip) from a skill folder.

Validates as the `portable` target first (packaging, claude.ai upload and the Skills
API reject non-spec frontmatter keys), then archives the skill. Excludes __pycache__,
node_modules, *.pyc, .DS_Store and a root evals/ directory. Entries are written in
sorted order with fixed timestamps, so the same tree gives the same archive.

Usage:
    uv run package_skill.py <skill-folder> [--output <dir>] [--json]
"""

import json
import os
import stat
import sys
import zipfile
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validation  # noqa: E402

FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def collect_files(root: Path) -> dict[str, Path]:
    """Map archive path (relative to root's parent) to the real file whose content is archived.

    A symlink resolving inside root is archived as its target's content. One escaping
    the root, or dangling, raises ValueError. Symlink loops raise ValueError.
    """
    found: dict[str, Path] = {}

    def visit(directory: Path, rel: tuple[str, ...], chain: tuple[Path, ...]) -> None:
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            path = Path(entry.path)
            parts = rel + (entry.name,)
            real = path.resolve()
            if path.is_symlink() and not (real.is_relative_to(root) and real.exists()):
                raise ValueError(f"Symlink escapes the skill root or is dangling: {'/'.join(parts)}")
            is_dir = real.is_dir()
            if validation.is_excluded(parts, is_dir):
                continue
            if is_dir:
                if real in chain:
                    raise ValueError(f"Symlink loop at: {'/'.join(parts)}")
                visit(path, parts, chain + (real,))
            elif real.is_file():
                found[(Path(root.name) / Path(*parts)).as_posix()] = real

    visit(root, (), (root,))
    return found


def package_skill(skill_path: Path, output_dir: Path | None = None) -> tuple[bool, str, dict]:
    """
    Package a skill folder into a .skill file.

    Args:
        skill_path: Path to the skill folder
        output_dir: Output directory (defaults to the current directory)

    Returns:
        Tuple of (success, message, details_dict)
    """
    skill_path = Path(skill_path).resolve()
    details = {"skill_path": str(skill_path), "files_added": []}

    if not skill_path.is_dir():
        return False, f"Skill folder not found: {skill_path}", details

    result = validation.validate_skill(skill_path, "portable")
    details["validation"] = result.details
    details["errors"] = result.errors
    details["warnings"] = result.warnings
    if not result.is_valid:
        return False, f"Validation failed: {result.message}", details

    out_dir = Path(output_dir).resolve() if output_dir else Path.cwd().resolve()
    if out_dir == skill_path or out_dir.is_relative_to(skill_path):
        return False, (f"Output directory {out_dir} is inside the skill being packaged; "
                       "choose a directory outside it with --output"), details

    try:
        files = collect_files(skill_path)
    except ValueError as e:
        return False, str(e), details

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{skill_path.name}.skill"
    details["output_file"] = str(target)
    # Write to a temp file and replace atomically so a failure keeps any existing package.
    tmp = out_dir / f".{target.name}.{os.getpid()}.tmp"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for arcname in sorted(files):
                src = files[arcname]
                info = zipfile.ZipInfo(arcname, FIXED_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IMODE(src.stat().st_mode) | stat.S_IFREG) << 16
                zf.writestr(info, src.read_bytes())
                details["files_added"].append(arcname)
        os.replace(tmp, target)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return False, f"Error creating .skill file: {e}", details

    details["file_count"] = len(files)
    return True, f"Successfully packaged skill to: {target}", details


@click.command()
@click.argument("skill_folder", type=click.Path(exists=True, file_okay=False, dir_okay=True))
@click.option("--output", "-o", type=click.Path(file_okay=False),
              help="Output directory for the .skill file (defaults to current directory; must be outside the skill)")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def main(skill_folder: str, output: str | None, output_json: bool):
    """
    Package a skill folder into a distributable .skill file.

    SKILL_FOLDER is the path to the skill directory to package. The skill is validated
    as the portable target first; any error stops packaging.
    """
    try:
        success, message, details = package_skill(Path(skill_folder), Path(output) if output else None)
    except Exception as e:
        if output_json:
            click.echo(json.dumps({"success": False, "error": str(e)}))
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if output_json:
        click.echo(json.dumps({"success": success, "message": message, "details": details}, indent=2))
    elif success:
        for w in details["warnings"]:
            click.echo(f"  warning [{w['check']}]: {w['message']}")
        for f in details["files_added"]:
            click.echo(f"   Added: {f}")
        click.echo(f"\nOK: {message}")
        click.echo(f"   Files packaged: {details['file_count']}")
    else:
        click.echo(f"Error: {message}", err=True)
        for e in details.get("errors", []):
            click.echo(f"  error [{e['check']}]: {e['message']}", err=True)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

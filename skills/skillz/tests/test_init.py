"""Tests for init_skill.py (criteria 12, 13; spec 2.5)."""

import json
import re
import subprocess
import sys
from pathlib import Path

import init_skill
import validation

SCRIPT = Path(init_skill.__file__).resolve()


def run(*args, cwd):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], cwd=cwd, capture_output=True, text=True)


def entries(d):
    return sorted(p.name for p in Path(d).iterdir())


def test_c12_no_with_creates_exactly_skill_md(tmp_path):
    p = run("my-skill", "--path", tmp_path / "out", cwd=tmp_path)
    assert p.returncode == 0, p.stderr
    assert entries(tmp_path / "out" / "my-skill") == ["SKILL.md"]


def test_c12_with_scripts_creates_exactly_skill_md_and_scripts(tmp_path):
    p = run("my-skill", "--path", tmp_path, "--with", "scripts", cwd=tmp_path)
    assert p.returncode == 0, p.stderr
    d = tmp_path / "my-skill"
    assert entries(d) == ["SKILL.md", "scripts"] and list((d / "scripts").iterdir()) == []


def test_c12_with_all_three(tmp_path):
    assert run("my-skill", "--path", tmp_path, "--with", "scripts, references,assets", cwd=tmp_path).returncode == 0
    assert entries(tmp_path / "my-skill") == ["SKILL.md", "assets", "references", "scripts"]


def test_c12_invalid_name_creates_nothing(tmp_path):
    out = tmp_path / "out"
    for bad in ["Bad_Name", "x-", "a--b", "x" * 65, ""]:
        p = run(bad, "--path", out, cwd=tmp_path)
        assert p.returncode == 1, bad
    assert not out.exists() and entries(tmp_path) == []


def test_c12_invalid_with_value_creates_nothing(tmp_path):
    p = run("my-skill", "--path", tmp_path, "--with", "scripts,bogus", cwd=tmp_path)
    assert p.returncode == 1 and entries(tmp_path) == []


def test_existing_directory_refused(tmp_path):
    (tmp_path / "my-skill").mkdir()
    assert run("my-skill", "--path", tmp_path, cwd=tmp_path).returncode == 1


def test_help_says_64_chars(tmp_path):
    p = run("--help", cwd=tmp_path)
    assert "64" in p.stdout and "40" not in p.stdout


def test_json_output(tmp_path):
    p = run("my-skill", "--path", tmp_path, "--json", cwd=tmp_path)
    out = json.loads(p.stdout)
    assert out["success"] and out["details"]["created_files"] == ["SKILL.md"]
    bad = json.loads(run("Bad", "--path", tmp_path, "--json", cwd=tmp_path).stdout)
    assert not bad["success"]


def test_template_content(tmp_path):
    init_skill.init_skill("my-skill", str(tmp_path))
    text = (tmp_path / "my-skill" / "SKILL.md").read_text()
    assert "When to use this skill" not in text
    assert not re.search(r"\b(ALWAYS|NEVER)\b", text)
    assert re.search(r'description: "TODO: State what the skill does and when to use it', text)
    assert "Resources" not in text


def test_generated_skill_has_no_errors_only_todo_warning(tmp_path):
    init_skill.init_skill("my-skill", str(tmp_path))
    r = validation.validate_skill(tmp_path / "my-skill", "portable")
    assert r.is_valid, r.errors
    assert "todo_placeholders" in {w["check"] for w in r.warnings}

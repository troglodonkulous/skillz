"""Tests for validation.py and quick_validate.py. Names cite acceptance criteria (spec 486-501)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import validation
from validation import validate_skill

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
GOOD_DESC = "Extracts tables from PDF files. Use when the user asks to pull tables out of a PDF."


def make_skill(tmp_path, frontmatter=None, body="# Skill\n\nBody.\n", name="my-skill", raw=None):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    if raw is None:
        fm = {"name": name, "description": GOOD_DESC} if frontmatter is None else frontmatter
        lines = []
        for k, v in fm.items():
            lines.append(f"{k}: {v}" if isinstance(v, str) and "\n" not in v and not v.startswith(("{", "[", "'", '"')) and v != ""
                         else f"{k}: {json.dumps(v)}")
        raw = "---\n" + "\n".join(lines) + "\n---\n" + body
    (d / "SKILL.md").write_text(raw)
    return d


def ids(findings):
    return {f["check"] for f in findings}


def test_c7_valid_frontmatter(tmp_path):
    for target in validation.TARGETS:
        r = validate_skill(make_skill(tmp_path), target)
        assert r.is_valid and not r.errors and not r.warnings, (target, r.errors, r.warnings)


@pytest.mark.parametrize("key", ["name", "description"])
def test_c7_missing_key_error_portable_warning_claude_code(tmp_path, key):
    fm = {"name": "my-skill", "description": GOOD_DESC}
    del fm[key]
    d = make_skill(tmp_path, fm)
    p = validate_skill(d, "portable")
    assert not p.is_valid and f"{key}_required" in ids(p.errors)
    c = validate_skill(d, "claude-code")
    assert c.is_valid and f"{key}_required" in ids(c.warnings)


def test_c7_missing_frontmatter_portable_error_claude_code_warning(tmp_path):
    d = make_skill(tmp_path, raw="# just a body\n")
    assert not validate_skill(d, "portable").is_valid
    c = validate_skill(d, "claude-code")
    assert c.is_valid and "frontmatter_exists" in ids(c.warnings)


@pytest.mark.parametrize("fm", [
    {"name": "", "description": GOOD_DESC},
    {"name": "   ", "description": GOOD_DESC},
    {"name": 5, "description": GOOD_DESC},
    {"name": "my-skill", "description": ""},
    {"name": "my-skill", "description": 12},
    {"name": "my-skill", "description": ["a"]},
])
def test_c7_empty_or_nonstring_name_description_errors_both(tmp_path, fm):
    d = make_skill(tmp_path, fm)
    for target in validation.TARGETS:
        assert not validate_skill(d, target).is_valid


def test_c7_malformed_frontmatter_errors(tmp_path):
    for raw in ["---\nname: [unclosed\n---\n", "---\n- a\n- b\n---\n", "---\nname: x\nno close\n", "---\n---\nbody"]:
        d = make_skill(tmp_path, raw=raw)
        assert not validate_skill(d, "claude-code").is_valid, raw


def test_c7_missing_skill_md(tmp_path):
    (tmp_path / "empty").mkdir()
    r = validate_skill(tmp_path / "empty")
    assert not r.is_valid and "skill_md_exists" in ids(r.errors)


@pytest.mark.parametrize("bad", ["Bad_Name", "-lead", "trail-", "dou--ble", "x" * 65, "UPPER"])
def test_c7_bad_names(tmp_path, bad):
    d = tmp_path / bad
    make_skill(tmp_path, {"name": bad, "description": GOOD_DESC}, name=bad)
    assert not validate_skill(d, "claude-code").is_valid


def test_c7_name_length_boundary(tmp_path):
    ok = "a" * 64
    assert validate_skill(make_skill(tmp_path, {"name": ok, "description": GOOD_DESC}, name=ok)).is_valid


def test_c7_description_length(tmp_path):
    at = "Extracts data. Use when asked. " + "x" * (1024 - 31)
    assert len(at) == 1024
    assert validate_skill(make_skill(tmp_path, {"name": "my-skill", "description": at}), "portable").is_valid
    over = at + "x"
    r = validate_skill(make_skill(tmp_path, {"name": "my-skill", "description": over}), "portable")
    assert "description_length" in ids(r.errors)


@pytest.mark.parametrize("ch", ["<", ">"])
def test_c7_angle_brackets(tmp_path, ch):
    r = validate_skill(make_skill(tmp_path, {"name": "my-skill", "description": f"Does a {ch} b. Use when x."}))
    assert "description_chars" in ids(r.errors)


def test_c7_disallowed_key_portable_error_claude_code_warning(tmp_path):
    d = make_skill(tmp_path, {"name": "my-skill", "description": GOOD_DESC, "allowed_tools": "Read"})
    assert "allowed_properties" in ids(validate_skill(d, "portable").errors)
    c = validate_skill(d, "claude-code")
    assert c.is_valid and "allowed_properties" in ids(c.warnings)


@pytest.mark.parametrize("value,ok", [("x", True), ("y" * 500, True), ("y" * 501, False),
                                      ('""', False), ('"   "', False), ("123", False), ("[a]", False)])
def test_c7_compatibility(tmp_path, value, ok):
    raw = f"---\nname: my-skill\ndescription: {GOOD_DESC}\ncompatibility: {value}\n---\nbody\n"
    d = make_skill(tmp_path, raw=raw)
    for target in validation.TARGETS:
        assert validate_skill(d, target).is_valid is ok, (target, value)


def test_c7_name_vs_directory(tmp_path):
    d = make_skill(tmp_path, {"name": "other-name", "description": GOOD_DESC})
    assert "name_matches_directory" in ids(validate_skill(d, "portable").errors)
    c = validate_skill(d, "claude-code")
    assert c.is_valid and "name_matches_directory" in ids(c.warnings)


def test_c7_broken_local_reference(tmp_path):
    d = make_skill(tmp_path, body="See [guide](references/guide.md).\n")
    for target in validation.TARGETS:
        assert "references_exist" in ids(validate_skill(d, target).errors)
    (d / "references").mkdir()
    (d / "references" / "guide.md").write_text("# G\n")
    assert validate_skill(d).is_valid


def test_c7_escaping_symlink(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    d = make_skill(tmp_path)
    (d / "assets").mkdir()
    (d / "assets" / "leak.txt").symlink_to(outside)
    for target in validation.TARGETS:
        r = validate_skill(d, target)
        assert not r.is_valid and "file_tree" in ids(r.errors)


def test_c7_internal_symlink_ok(tmp_path):
    d = make_skill(tmp_path)
    (d / "references").mkdir()
    (d / "references" / "a.md").write_text("# A\n")
    (d / "references" / "b.md").symlink_to(d / "references" / "a.md")
    assert "file_tree" not in ids(validate_skill(d).errors)


def test_c8_portable_rejects_argument_hint_claude_code_accepts_with_notice(tmp_path):
    d = make_skill(tmp_path, {"name": "my-skill", "description": GOOD_DESC, "argument-hint": "[file]"})
    assert not validate_skill(d, "portable").is_valid
    r = validate_skill(d, "claude-code")
    assert r.is_valid and ids(r.notices) == {"portability"} and "argument-hint" in r.notices[0]["message"]
    assert not r.warnings


def run_cli(*args, cwd):
    return subprocess.run([sys.executable, str(SCRIPTS / "quick_validate.py"), *map(str, args)],
                          cwd=cwd, capture_output=True, text=True)


def test_c8_cli_default_target_and_strict_keeps_notice(tmp_path):
    d = make_skill(tmp_path, {"name": "my-skill", "description": GOOD_DESC, "argument-hint": "[file]"})
    p = run_cli(d, "--json", cwd=tmp_path)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["valid"] and out["notices"][0]["check"] == "portability"
    s = run_cli(d, "--strict", "--json", cwd=tmp_path)
    assert s.returncode == 0 and json.loads(s.stdout)["notices"]
    assert run_cli(d, "--target", "portable", cwd=tmp_path).returncode == 1


def test_strict_promotes_warnings(tmp_path):
    d = make_skill(tmp_path, body="TODO fix\n")
    assert run_cli(d, cwd=tmp_path).returncode == 0
    assert run_cli(d, "--strict", cwd=tmp_path).returncode == 1


def test_c9_placeholders_pass_and_missing_skill_dir_ref_fails(tmp_path):
    body = ("Run ${CLAUDE_SKILL_DIR}/scripts/run.py with ${CLAUDE_PROJECT_DIR}/config.yml and $ARGUMENTS.\n"
            "Also ${CLAUDE_PLUGIN_ROOT}/x.sh and [web](https://example.com/a) and [top](#anchor) and "
            "[abs](/etc/hosts) and [m](mailto:a@b.c).\n")
    d = make_skill(tmp_path, body=body)
    (d / "scripts").mkdir()
    (d / "scripts" / "run.py").write_text("print(1)\n")
    r = validate_skill(d)
    assert r.is_valid, r.errors
    bad = make_skill(tmp_path, body="Run ${CLAUDE_SKILL_DIR}/scripts/gone.py\n", name="bad-skill")
    assert "references_exist" in ids(validate_skill(bad).errors)


def test_c9_extraction_is_conservative():
    text = ("```\n[x](references/nope.md)\n`scripts/nope.py`\n```\n"
            "`[y](references/nope2.md)` and `scripts/<name>.py` and `uv run scripts/a.py` and "
            "[tpl](assets/{{x}}.md) [anchor](references/a.md#sec) [q](<references/b.md> \"t\")\n")
    assert validation.extract_references(text) == ["references/a.md", "references/b.md"]


def test_c9_extraction_kinds():
    text = ("[l](./guide.md) `scripts/run.py` ${CLAUDE_SKILL_DIR}/assets/t.txt, "
            "![img](assets/p.png) $NAME ${OTHER}/x\n")
    refs = validation.extract_references(text)
    assert sorted(refs) == sorted(["assets/t.txt", "./guide.md", "scripts/run.py", "assets/p.png"])


def test_c10_listing_length_warning_claude_code_only(tmp_path):
    d = make_skill(tmp_path, {"name": "my-skill", "description": GOOD_DESC, "when_to_use": "w" * 1500})
    c = validate_skill(d, "claude-code")
    assert c.is_valid and "listing_length" in ids(c.warnings)
    short = make_skill(tmp_path, {"name": "my-skill", "description": GOOD_DESC, "when_to_use": "w" * 100})
    assert "listing_length" not in ids(validate_skill(short).warnings)
    assert "listing_length" not in ids(validate_skill(d, "portable").warnings)


def test_c6_validation_is_single_implementation():
    for name in ("quick_validate.py", "package_skill.py", "init_skill.py"):
        src = (SCRIPTS / name).read_text()
        assert "import validation" in src
        assert "yaml.safe_load" not in src and "ALLOWED_PROPERTIES" not in src
        assert "def validate_skill" not in src or name == "quick_validate.py"
    qv = (SCRIPTS / "quick_validate.py").read_text()
    assert "validation.validate_skill(" in qv


def test_parse_frontmatter_preserves_order_and_body():
    fm, body, err = validation.parse_frontmatter("---\nz: 1\na: 2\nname: x\n---\nhello\n")
    assert err is None and list(fm) == ["z", "a", "name"] and body == "hello\n"
    assert validation.parse_frontmatter("nothing")[2] is not None


def test_key_sets():
    assert validation.SPEC_KEYS == {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    assert validation.SPEC_KEYS < validation.CLAUDE_CODE_KEYS
    assert {"when_to_use", "paths", "hooks", "shell"} <= validation.CLAUDE_CODE_KEYS


def test_quality_warnings(tmp_path):
    body = "\n".join(f"line {i}" for i in range(600)) + "\nTODO\n"
    d = make_skill(tmp_path, {"name": "my-skill", "description": "Thing"}, body=body)
    (d / "README.md").write_text("x")
    (d / "scripts").mkdir()
    (d / "references").mkdir()
    (d / "references" / "long.md").write_text("\n".join("text" for _ in range(150)))
    w = ids(validate_skill(d).warnings)
    assert {"skill_md_lines", "todo_placeholders", "description_trigger", "auxiliary_docs",
            "empty_resource_dirs", "reference_toc", "unreferenced_files"} <= w


def test_body_token_warning(tmp_path):
    d = make_skill(tmp_path, body="word " * 5000)
    assert "body_tokens" in ids(validate_skill(d).warnings)


def test_toc_satisfies_long_reference(tmp_path):
    d = make_skill(tmp_path, body="See [r](references/long.md).\n")
    (d / "references").mkdir()
    (d / "references" / "long.md").write_text("# Long\n\n## Contents\n- a\n\n" + "\n".join("t" for _ in range(150)))
    assert "reference_toc" not in ids(validate_skill(d).warnings)


def test_unreferenced_one_hop_and_directory_cover(tmp_path):
    d = make_skill(tmp_path, body="See [r](references/r.md) and `assets/` and ${CLAUDE_SKILL_DIR}/scripts/a.py\n")
    (d / "references").mkdir()
    (d / "references" / "r.md").write_text("Deeper: [h](hop.md) and `scripts/b.py`\n")
    (d / "references" / "hop.md").write_text("# hop\n")
    (d / "references" / "orphan.md").write_text("# orphan\n")
    (d / "assets").mkdir()
    (d / "assets" / "sub").mkdir()
    (d / "assets" / "sub" / "f.txt").write_text("x")
    (d / "scripts").mkdir()
    (d / "scripts" / "a.py").write_text("")
    (d / "scripts" / "b.py").write_text("")
    (d / "scripts" / "c.py").write_text("")
    msg = next(w["message"] for w in validate_skill(d).warnings if w["check"] == "unreferenced_files")
    assert "references/orphan.md" in msg and "scripts/c.py" in msg
    for covered in ("hop.md", "scripts/b.py", "scripts/a.py", "assets/sub/f.txt", "references/r.md"):
        assert covered not in msg.replace("references/orphan.md", "").replace("scripts/c.py", ""), covered


def test_excluded_files_ignored_by_scan(tmp_path):
    d = make_skill(tmp_path)
    (d / "scripts" / "__pycache__").mkdir(parents=True)
    (d / "scripts" / "__pycache__" / "x.pyc").write_text("")
    (d / "scripts" / "run.py").write_text("")
    msg = [w["message"] for w in validate_skill(d).warnings if w["check"] == "unreferenced_files"]
    assert msg and "pycache" not in msg[0]


@pytest.mark.parametrize("padded", [" my-skill", "my-skill "])
def test_c7_padded_name_rejected(tmp_path, padded):
    d = make_skill(tmp_path, raw=f'---\nname: "{padded}"\ndescription: {GOOD_DESC}\n---\nbody\n')
    for target in validation.TARGETS:
        assert "name_format" in ids(validate_skill(d, target).errors)


def test_c7_escaping_symlink_inside_excluded_dir_still_error(tmp_path):
    (tmp_path / "secret").write_text("x")
    d = make_skill(tmp_path)
    (d / "node_modules").mkdir()
    (d / "node_modules" / "l").symlink_to(tmp_path / "secret")
    assert "file_tree" in ids(validate_skill(d).errors)


def test_c7_empty_mapping_frontmatter_warns_missing_fields(tmp_path):
    d = make_skill(tmp_path, raw="---\n{}\n---\nbody\n")
    c = validate_skill(d, "claude-code")
    assert c.is_valid and {"name_required", "description_required"} <= ids(c.warnings)
    assert {"name_required", "description_required"} <= ids(validate_skill(d, "portable").errors)


def test_c9_link_destination_with_parentheses(tmp_path):
    assert validation.extract_references("[x](scripts/foo(1).py) and [y](a.md)") == ["scripts/foo(1).py", "a.md"]
    d = make_skill(tmp_path, body="Run [it](scripts/foo(1).py).\n")
    (d / "scripts").mkdir()
    (d / "scripts" / "foo(1).py").write_text("")
    assert validate_skill(d).is_valid


def test_c9_skill_dir_placeholder_with_parentheses(tmp_path):
    assert validation.extract_references("(run ${CLAUDE_SKILL_DIR}/scripts/foo(1).py)") == ["scripts/foo(1).py"]
    d = make_skill(tmp_path, body="Run ${CLAUDE_SKILL_DIR}/scripts/foo(1).py now.\n")
    (d / "scripts").mkdir()
    (d / "scripts" / "foo(1).py").write_text("")
    assert validate_skill(d).is_valid


def test_c9_nonexistent_inline_code_path_is_not_an_error(tmp_path):
    d = make_skill(tmp_path, body="Example: `scripts/rotate_pdf.py` and `references/nope.md`.\n")
    r = validate_skill(d)
    assert r.is_valid and "references_exist" not in ids(r.errors)
    assert validate_skill(d, "portable").is_valid


def test_c9_inline_code_path_still_marks_real_file_referenced(tmp_path):
    d = make_skill(tmp_path, body="Use `scripts/rotate_pdf.py`.\n")
    (d / "scripts").mkdir()
    (d / "scripts" / "rotate_pdf.py").write_text("")
    assert "unreferenced_files" not in ids(validate_skill(d).warnings)


def test_c7_space_padded_description_over_limit_rejected(tmp_path):
    d = make_skill(tmp_path, raw=f'---\nname: my-skill\ndescription: "{GOOD_DESC}{" " * 1100}"\n---\nbody\n')
    for target in validation.TARGETS:
        assert "description_length" in ids(validate_skill(d, target).errors)

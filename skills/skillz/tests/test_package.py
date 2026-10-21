"""Tests for package_skill.py (criteria 8, 11, 13; spec 2.5, 4.3)."""

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import package_skill

SCRIPT = Path(package_skill.__file__).resolve()
DESC = "Extracts tables from PDF files. Use when the user asks to pull tables out of a PDF."


def make_skill(tmp_path, extra_fm="", name="pkg-skill"):
    d = tmp_path / "src" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {DESC}\n{extra_fm}---\n\nSee [r](references/r.md) and `scripts/run.py`.\n")
    (d / "references").mkdir()
    (d / "references" / "r.md").write_text("# R\n")
    (d / "scripts").mkdir()
    (d / "scripts" / "run.py").write_text("print(1)\n")
    return d


def run(skill, *args, cwd):
    return subprocess.run([sys.executable, str(SCRIPT), str(skill), *map(str, args)], cwd=cwd, capture_output=True, text=True)


def names(archive):
    with zipfile.ZipFile(archive) as z:
        return z.namelist()


def test_c11_excludes_and_sorted_order(tmp_path):
    d = make_skill(tmp_path)
    (d / "scripts" / "__pycache__").mkdir()
    (d / "scripts" / "__pycache__" / "run.cpython-312.pyc").write_bytes(b"x")
    (d / "scripts" / "stale.pyc").write_bytes(b"x")
    (d / ".DS_Store").write_bytes(b"x")
    (d / "references" / ".DS_Store").write_bytes(b"x")
    (d / "node_modules" / "pkg").mkdir(parents=True)
    (d / "node_modules" / "pkg" / "i.js").write_text("x")
    (d / "evals").mkdir()
    (d / "evals" / "evals.json").write_text("{}")
    (d / "assets").mkdir()
    (d / "assets" / "evals").mkdir()
    (d / "assets" / "evals" / "keep.txt").write_text("nested evals dir is kept")
    out = tmp_path / "dist"
    p = run(d, "--output", out, cwd=tmp_path)
    assert p.returncode == 0, p.stderr
    got = names(out / "pkg-skill.skill")
    assert got == sorted(got)
    assert got == ["pkg-skill/SKILL.md", "pkg-skill/assets/evals/keep.txt",
                   "pkg-skill/references/r.md", "pkg-skill/scripts/run.py"]


def test_c11_deterministic_bytes(tmp_path):
    d = make_skill(tmp_path)
    run(d, "-o", tmp_path / "a", cwd=tmp_path)
    os.utime(d / "SKILL.md", (1, 1))
    run(d, "-o", tmp_path / "b", cwd=tmp_path)
    assert (tmp_path / "a" / "pkg-skill.skill").read_bytes() == (tmp_path / "b" / "pkg-skill.skill").read_bytes()


def test_c11_refuses_output_inside_source_tree(tmp_path):
    d = make_skill(tmp_path)
    for out in (d, d / "dist", d / "references" / ".." / "x"):
        p = run(d, "--output", out, cwd=tmp_path)
        assert p.returncode == 1 and "inside" in p.stderr
    assert not (d / "dist").exists() and not (d / "x").exists() and not list(d.glob("*.skill"))


def test_c11_refuses_default_output_when_cwd_inside_source(tmp_path):
    d = make_skill(tmp_path)
    p = run(d, cwd=d)
    assert p.returncode == 1 and not list(d.glob("*.skill"))


def test_c11_output_via_symlink_into_tree_refused(tmp_path):
    d = make_skill(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(d)
    assert run(d, "--output", link / "dist", cwd=tmp_path).returncode == 1


def test_c11_default_output_is_cwd(tmp_path):
    d = make_skill(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    assert run(d, cwd=work).returncode == 0 and (work / "pkg-skill.skill").exists()


def test_c8_rejects_claude_code_key(tmp_path):
    d = make_skill(tmp_path, extra_fm="argument-hint: '[file]'\n")
    p = run(d, "-o", tmp_path / "o", "--json", cwd=tmp_path)
    out = json.loads(p.stdout)
    assert p.returncode == 1 and not out["success"] and not (tmp_path / "o").exists()
    assert out["details"]["errors"][0]["check"] == "allowed_properties"


def test_packages_as_portable_name_must_match_dir(tmp_path):
    d = make_skill(tmp_path)
    (d / "SKILL.md").write_text((d / "SKILL.md").read_text().replace("name: pkg-skill", "name: other"))
    assert run(d, "-o", tmp_path / "o", cwd=tmp_path).returncode == 1


def test_symlink_inside_root_archived_as_content(tmp_path):
    d = make_skill(tmp_path)
    (d / "references" / "alias.md").symlink_to(d / "references" / "r.md")
    run(d, "-o", tmp_path / "o", cwd=tmp_path)
    with zipfile.ZipFile(tmp_path / "o" / "pkg-skill.skill") as z:
        assert z.read("pkg-skill/references/alias.md") == b"# R\n"
        assert z.getinfo("pkg-skill/references/alias.md").external_attr >> 28 == 0o10  # regular file


def test_symlink_escaping_root_is_error(tmp_path):
    d = make_skill(tmp_path)
    (tmp_path / "secret").write_text("s")
    (d / "assets").mkdir()
    (d / "assets" / "leak").symlink_to(tmp_path / "secret")
    p = run(d, "-o", tmp_path / "o", cwd=tmp_path)
    assert p.returncode == 1 and not (tmp_path / "o" / "pkg-skill.skill").exists()


def test_symlink_loop_is_error_not_hang(tmp_path):
    d = make_skill(tmp_path)
    (d / "assets").mkdir()
    (d / "assets" / "loop").symlink_to(d / "assets")
    p = run(d, "-o", tmp_path / "o", cwd=tmp_path)
    assert p.returncode == 1 and "loop" in p.stderr.lower()


def test_escaping_symlink_in_excluded_dir_is_still_error(tmp_path):
    d = make_skill(tmp_path)
    (tmp_path / "outside").write_text("x")
    (d / "node_modules").mkdir()
    (d / "node_modules" / "bin").symlink_to(tmp_path / "outside")
    assert run(d, "-o", tmp_path / "o", cwd=tmp_path).returncode == 1


def test_failed_packaging_keeps_existing_archive(tmp_path, monkeypatch):
    d = make_skill(tmp_path)
    out = tmp_path / "o"
    assert package_skill.package_skill(d, out)[0]
    before = (out / "pkg-skill.skill").read_bytes()
    monkeypatch.setattr(package_skill.zipfile.ZipFile, "writestr",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    ok, msg, _ = package_skill.package_skill(d, out)
    assert not ok and (out / "pkg-skill.skill").read_bytes() == before
    assert sorted(p.name for p in out.iterdir()) == ["pkg-skill.skill"]


def test_c13_package_runs_from_unrelated_cwd_and_uv_smoke(tmp_path):
    d = make_skill(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert run(d, "-o", tmp_path / "o", cwd=elsewhere).returncode == 0


def test_c13_uv_run_smoke(tmp_path):
    p = subprocess.run(["uv", "run", str(SCRIPT), "--help"], cwd=tmp_path, capture_output=True, text=True)
    assert p.returncode == 0 and "--output" in p.stdout and "--json" in p.stdout


def test_c13_all_scripts_help_from_unrelated_cwd(tmp_path):
    for name in ("quick_validate.py", "init_skill.py", "package_skill.py"):
        p = subprocess.run([sys.executable, str(SCRIPT.parent / name), "--help"], cwd=tmp_path, capture_output=True, text=True)
        assert p.returncode == 0 and "--json" in p.stdout, name


def test_c13_quick_validate_and_init_from_unrelated_cwd(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    i = subprocess.run([sys.executable, str(SCRIPT.parent / "init_skill.py"), "s-one", "--path", str(tmp_path / "sk")],
                       cwd=work, capture_output=True, text=True)
    assert i.returncode == 0, i.stderr
    q = subprocess.run([sys.executable, str(SCRIPT.parent / "quick_validate.py"), str(tmp_path / "sk" / "s-one"), "--json"],
                       cwd=work, capture_output=True, text=True)
    assert q.returncode == 0 and json.loads(q.stdout)["valid"]

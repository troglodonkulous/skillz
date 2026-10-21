# /// script
# dependencies = ["pyyaml"]
# ///
"""Shared skill validation. The only implementation of validation logic.

quick_validate.py, package_skill.py and init_skill.py import this module.
Frontmatter checks depend on the target: `claude-code` (default) accepts Claude Code's
own fields, `portable` accepts only the Agent Skills spec fields that claude.ai upload
and the Skills API require. Errors block, warnings and notices are reported.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

import yaml

SPEC_KEYS = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)
CLAUDE_CODE_KEYS = SPEC_KEYS | frozenset(
    {
        "when_to_use", "argument-hint", "arguments", "disable-model-invocation",
        "user-invocable", "disallowed-tools", "model", "effort", "context", "agent",
        "background", "hooks", "paths", "shell",
    }
)
TARGETS = ("claude-code", "portable")

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_COMPATIBILITY_LENGTH = 500
MAX_LISTING_LENGTH = 1536  # description + when_to_use, Claude Code skill listing cap
MAX_SKILL_MD_LINES = 500
MAX_BODY_TOKENS = 5000
CHARS_PER_TOKEN = 4  # heuristic: the body token estimate is len(body) / 4
MAX_REFERENCE_LINES = 100

RESOURCE_DIRS = ("scripts", "references", "assets", "agents")
AUXILIARY_DOCS = ("README.md", "CHANGELOG.md", "INSTALLATION_GUIDE.md", "QUICK_REFERENCE.md")

# Packaging exclusions (official's list). ROOT_EXCLUDE_DIRS apply at the skill root only.
EXCLUDE_DIRS = frozenset({"__pycache__", "node_modules"})
EXCLUDE_GLOBS = ("*.pyc",)
EXCLUDE_FILES = frozenset({".DS_Store"})
ROOT_EXCLUDE_DIRS = frozenset({"evals"})


def is_excluded(rel_parts: tuple[str, ...], is_dir: bool = False) -> bool:
    """True if a path (parts relative to the skill root) is left out of packages."""
    if not rel_parts:
        return False
    dir_parts = rel_parts if is_dir else rel_parts[:-1]
    if any(p in EXCLUDE_DIRS for p in dir_parts):
        return True
    if rel_parts[0] in ROOT_EXCLUDE_DIRS and (is_dir or len(rel_parts) > 1):
        return True
    if is_dir:
        return False
    name = rel_parts[-1]
    return name in EXCLUDE_FILES or any(fnmatch.fnmatch(name, g) for g in EXCLUDE_GLOBS)


# --------------------------------------------------------------------------- frontmatter

def skill_tree_conflict(path: str | Path) -> str | None:
    """Why nothing may be written at `path`, or None. Skill discovery reads a
    `.claude/skills` tree and a plugin's `skills/` directory, and every skill
    directory is someone's source tree; eval output in any of them would be
    discovered, packaged or committed. Symlinks are resolved first."""
    resolved = Path(path).resolve()
    for parent in (resolved, *resolved.parents):
        if (parent / "SKILL.md").is_file():
            return f"{parent} is a skill directory"
        if parent.name == "skills" and (parent.parent.name == ".claude"
                                        or (parent.parent / ".claude-plugin").is_dir()):
            return f"{parent} is a skills directory"
    return None


def parse_frontmatter(text: str) -> tuple[dict | None, str, str | None]:
    """Parse SKILL.md text into (frontmatter, body, error).

    On success error is None and frontmatter keeps every key in file order.
    On failure frontmatter is None and error says why. Text without a leading
    '---' line is an error ("No YAML frontmatter found").
    """
    text = text.lstrip("﻿")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, text, "No YAML frontmatter found"
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None, text, "Invalid frontmatter format: no closing '---'"
    body = "".join(lines[end + 1:])
    try:
        data = yaml.safe_load("".join(lines[1:end]))
    except yaml.YAMLError as e:
        return None, body, f"Invalid YAML in frontmatter: {e}"
    if not isinstance(data, dict):
        return None, body, "Frontmatter must be a YAML mapping"
    return data, body, None


def check_name(name: str) -> str | None:
    """Return an error message if name breaks the naming rules, else None."""
    if not name.strip():
        return "Name cannot be empty or whitespace-only; use a valid hyphen-case identifier"
    if not re.fullmatch(r"[a-z0-9-]+", name):
        return f"Name '{name}' should be hyphen-case (lowercase letters, digits, and hyphens only)"
    if name.startswith("-") or name.endswith("-") or "--" in name:
        return f"Name '{name}' cannot start/end with hyphen or contain consecutive hyphens"
    if len(name) > MAX_NAME_LENGTH:
        return f"Name is too long ({len(name)} characters). Maximum is {MAX_NAME_LENGTH} characters."
    return None


# --------------------------------------------------------------------------- references

_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_LINK = re.compile(r"!?\[[^\]\n]*\]\(((?:[^()\n]|\([^()\n]*\))*)\)")  # allows one level of ()
_MD_DEF = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*(\S+)", re.MULTILINE)
_SKILL_DIR_REF = re.compile(r"\$\{CLAUDE_SKILL_DIR\}/((?:[^\s`'\"()\[\]<>]|\([^()\s]*\))+)")
_CODE_PATH = re.compile(r"(?:%s)/[A-Za-z0-9_.\-/]*" % "|".join(RESOURCE_DIRS))
_PLACEHOLDER_CHARS = re.compile(r"[<>*{}$|]")


def _strip_fences(text: str) -> str:
    out, fence = [], None
    for line in text.splitlines():
        m = _FENCE.match(line)
        if m and (fence is None or fence == m.group(1)):
            fence = None if fence else m.group(1)
            out.append("")
        else:
            out.append("" if fence else line)
    return "\n".join(out)


def _clean(ref: str, drop_anchor: bool = True) -> str | None:
    if drop_anchor:
        ref = re.split(r"[#?]", ref, maxsplit=1)[0]
    ref = unquote(ref).strip().rstrip(".,;:!")
    if not ref or _PLACEHOLDER_CHARS.search(ref) or " " in ref:
        return None
    return ref


def extract_references(text: str, inline_code: bool = True) -> list[str]:
    """Bundled-file references in markdown text, as skill-root-relative posix paths.

    Counts relative markdown link targets, `${CLAUDE_SKILL_DIR}/<path>`, and inline-code
    paths starting with scripts/, references/, assets/ or agents/. Runtime placeholders
    (`${CLAUDE_PROJECT_DIR}`, `$ARGUMENTS`, ...) are never references. Markdown link and
    inline-code forms inside fenced code blocks are examples, not references.
    """
    refs: list[str] = []
    for m in _SKILL_DIR_REF.finditer(text):
        ref = _clean(m.group(1))
        if ref:
            refs.append(ref)
    prose = _strip_fences(text)
    for span in _INLINE_CODE.findall(prose) if inline_code else ():
        span = span.strip()
        if _CODE_PATH.fullmatch(span):
            refs.append(span)
    no_code = _INLINE_CODE.sub(" ", prose)
    targets = [m.group(1).strip() for m in _MD_LINK.finditer(no_code)]
    targets += [m.group(1) for m in _MD_DEF.finditer(no_code)]
    for t in targets:
        if t.startswith("<") and ">" in t:
            t = t[1:t.index(">")]
        else:
            t = t.split()[0] if t.split() else ""
        if not t or t[0] in "#/$" or t.startswith("${") or _SCHEME.match(t):
            continue
        ref = _clean(t)
        if ref:
            refs.append(ref)
    return refs


def _norm(ref: str) -> str:
    return posixpath.normpath(ref.replace("\\", "/"))


# --------------------------------------------------------------------------- result

@dataclass
class ValidationResult:
    """Outcome of validate_skill. Findings are {"check": id, "message": text} dicts."""
    skill_path: str
    target: str
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    notices: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    info: dict = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def message(self) -> str:
        if self.errors:
            return self.errors[0]["message"]
        return "Skill is valid!" if not self.warnings else f"Skill is valid ({len(self.warnings)} warning(s))"

    @property
    def details(self) -> dict:
        return {"skill_path": self.skill_path, "target": self.target, "checks": self.checks, **self.info}

    def as_tuple(self) -> tuple[bool, str, dict]:
        return self.is_valid, self.message, self.details

    def to_dict(self) -> dict:
        return {
            "valid": self.is_valid, "message": self.message, "errors": self.errors,
            "warnings": self.warnings, "notices": self.notices, "details": self.details,
        }


def apply_strict(result: ValidationResult) -> ValidationResult:
    """Promote warnings to errors (--strict). Notices stay notices."""
    result.errors = result.errors + result.warnings
    result.warnings = []
    return result


class _Collector:
    def __init__(self, result: ValidationResult):
        self.r = result

    def check(self, check_id: str, ok: bool, message: str = "", level: str = "errors") -> bool:
        self.r.checks.append({"check": check_id, "passed": ok})
        if not ok:
            getattr(self.r, level).append({"check": check_id, "message": message})
        return ok

    def notice(self, check_id: str, message: str) -> None:
        self.r.notices.append({"check": check_id, "message": message})


# --------------------------------------------------------------------------- validate

def validate_skill(path: str | Path, target: str = "claude-code") -> ValidationResult:
    """Validate a skill directory for target 'claude-code' or 'portable' (spec 4.3)."""
    if target not in TARGETS:
        raise ValueError(f"target must be one of {TARGETS}, got {target!r}")
    root = Path(path).resolve()
    result = ValidationResult(str(root), target)
    c = _Collector(result)
    portable = target == "portable"
    soft = "errors" if portable else "warnings"  # level for portable-only errors

    skill_md = root / "SKILL.md"
    if not c.check("skill_md_exists", skill_md.is_file(), "SKILL.md not found"):
        return result
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        c.check("skill_md_readable", False, f"Cannot read SKILL.md: {e}")
        return result

    fm, body, err = parse_frontmatter(text)
    has_fm = True
    if err is not None and err == "No YAML frontmatter found":
        c.check("frontmatter_exists", False,
                "No YAML frontmatter found" + ("" if portable else
                " (Claude Code falls back to the directory name and first body line)"), soft)
        fm, has_fm = {}, False
    elif not c.check("frontmatter_valid", err is None, err or ""):
        return result
    result.info["body_tokens_estimate"] = len(body) // CHARS_PER_TOKEN

    # key allow-lists
    keys = list(fm)
    if portable:
        bad = [k for k in keys if k not in SPEC_KEYS]
        c.check("allowed_properties", not bad,
                f"Unexpected key(s) in SKILL.md frontmatter: {', '.join(sorted(map(str, bad)))}. "
                f"Allowed properties are: {', '.join(sorted(SPEC_KEYS))}")
    else:
        bad = [k for k in keys if k not in CLAUDE_CODE_KEYS]
        c.check("allowed_properties", not bad,
                f"Unknown frontmatter key(s): {', '.join(sorted(map(str, bad)))} "
                "(Claude Code ignores unknown keys; check for typos)", "warnings")
        cc_only = [k for k in keys if k in CLAUDE_CODE_KEYS - SPEC_KEYS]
        if cc_only:
            c.notice("portability",
                     f"Claude Code-only key(s) present: {', '.join(cc_only)}. "
                     "This skill will not upload to claude.ai or the API until they are removed.")

    # name
    name = fm.get("name")
    name_ok = False
    if "name" not in fm:
        if has_fm or portable:
            c.check("name_required", False, "Missing 'name' in frontmatter", soft)
    elif not isinstance(name, str):
        c.check("name_type", False, f"Name must be a string, got {type(name).__name__}")
    else:
        problem = check_name(name)  # unstripped: padded names fail the regex
        name_ok = c.check("name_format", problem is None, problem or "")
        if name_ok:
            c.check("name_matches_directory", name == root.name,
                    f"Name '{name}' does not match directory name '{root.name}'", soft)
            result.info["name"] = name

    # description
    desc = fm.get("description")
    if "description" not in fm:
        if has_fm or portable:
            c.check("description_required", False, "Missing 'description' in frontmatter", soft)
    elif not isinstance(desc, str):
        c.check("description_type", False, f"Description must be a string, got {type(desc).__name__}")
    else:
        raw_desc, desc = desc, desc.strip()
        if c.check("description_non_empty", bool(desc), "Description cannot be empty or whitespace-only"):
            c.check("description_chars", "<" not in desc and ">" not in desc,
                    "Description cannot contain angle brackets (< or >)")
            c.check("description_length", len(raw_desc) <= MAX_DESCRIPTION_LENGTH,
                    f"Description is too long ({len(raw_desc)} characters). Maximum is {MAX_DESCRIPTION_LENGTH} characters.")
            result.info["description_length"] = len(desc)
            _description_quality(c, desc)

    if "compatibility" in fm:
        comp = fm["compatibility"]
        c.check("compatibility", isinstance(comp, str) and 1 <= len(comp.strip()) and len(comp) <= MAX_COMPATIBILITY_LENGTH,
                f"compatibility must be a non-empty string of 1-{MAX_COMPATIBILITY_LENGTH} characters")

    if not portable:
        parts = [v.strip() for v in (fm.get("description"), fm.get("when_to_use")) if isinstance(v, str)]
        total = sum(len(p) for p in parts)
        c.check("listing_length", total <= MAX_LISTING_LENGTH,
                f"description + when_to_use is {total} characters; Claude Code cuts the skill "
                f"listing at {MAX_LISTING_LENGTH}", "warnings")

    # SKILL.md size and leftovers
    n_lines = len(text.splitlines())
    c.check("skill_md_lines", n_lines <= MAX_SKILL_MD_LINES,
            f"SKILL.md is {n_lines} lines (recommended maximum {MAX_SKILL_MD_LINES})", "warnings")
    tokens = result.info["body_tokens_estimate"]
    c.check("body_tokens", tokens <= MAX_BODY_TOKENS,
            f"SKILL.md body is about {tokens} tokens (estimated as chars/{CHARS_PER_TOKEN}); "
            f"the tail may be dropped after compaction above {MAX_BODY_TOKENS}", "warnings")
    c.check("todo_placeholders", not re.search(r"\bTODO\b", text),
            "SKILL.md still contains TODO / template placeholder text", "warnings")

    _check_tree(c, root)
    _check_references(c, root, text)
    _check_resources(c, root, text)
    return result


_TRIGGER = re.compile(
    r"\b(use (this )?(skill )?(when|for|whenever|if)|use when|when(ever)? (the )?(user|you|asked|working|needing|someone)|"
    r"triggers?\b|if (the )?user|when\b)", re.IGNORECASE)


def _description_quality(c: _Collector, desc: str) -> None:
    m = _TRIGGER.search(desc)
    what = desc[: m.start()] if m else desc
    c.check("description_capability", len(what.split()) >= 3,
            "Description should say what the skill does", "warnings")
    c.check("description_trigger", m is not None,
            "Description should say when to use the skill (trigger context)", "warnings")


def _walk(root: Path, prune: bool = True):
    """Yield (path, rel_parts, is_dir) for every entry below root.

    With prune, packaging exclusions are skipped. Symlinked directories are yielded
    but not entered.
    """
    stack = [root]
    while stack:
        d = stack.pop()
        for entry in sorted(os.scandir(d), key=lambda e: e.name):
            p = Path(entry.path)
            rel = p.relative_to(root).parts
            is_dir = entry.is_dir(follow_symlinks=False)
            if prune and is_excluded(rel, is_dir):
                continue
            yield p, rel, is_dir
            if is_dir:
                stack.append(p)


def _check_tree(c: _Collector, root: Path) -> None:
    bad = []
    for p, rel, _ in _walk(root, prune=False):  # security check: excluded dirs included
        if p.is_symlink():
            real = p.resolve()
            if not real.is_relative_to(root):
                bad.append(f"{'/'.join(rel)} (symlink escapes the skill root)")
            elif not real.exists():
                bad.append(f"{'/'.join(rel)} (dangling symlink)")
    c.check("file_tree", not bad, "Bad file(s): " + "; ".join(bad))


def _check_references(c: _Collector, root: Path, text: str) -> None:
    missing = []
    for ref in dict.fromkeys(extract_references(text, inline_code=False)):
        norm = _norm(ref)
        if norm.startswith("..") or not (root / norm).exists():
            missing.append(ref)
    c.check("references_exist", not missing,
            "Referenced file(s) not found in the skill: " + ", ".join(missing))


def _resolve_ref(root: Path, base: Path, ref: str) -> str | None:
    """Skill-root-relative path of ref (relative to base dir, else to root) if it exists."""
    for start in (base, root):
        cand = Path(posixpath.normpath((start / ref).as_posix()))
        if cand.exists() and cand.is_relative_to(root):
            return cand.relative_to(root).as_posix()
    return None


def _has_toc(lines: list[str]) -> bool:
    head = "\n".join(lines[:40])
    return bool(re.search(r"^#+\s*(table of )?contents\b|^\**(table of )?contents\**:?\s*$", head, re.I | re.M)
                or len(re.findall(r"\]\(#", head)) >= 3)


def _check_resources(c: _Collector, root: Path, skill_text: str) -> None:
    # referenced set: SKILL.md, plus one hop through the files it references
    referenced: set[str] = set()
    hop_files: list[Path] = []
    for ref in extract_references(skill_text):
        rel = _resolve_ref(root, root, ref)
        if rel is None:
            continue
        referenced.add(rel)
        target = root / rel
        if target.is_file():
            hop_files.append(target)
        elif target.is_dir():
            hop_files += [p for p, _, d in _walk(target) if not d]
    for f in hop_files:
        if f.suffix.lower() not in (".md", ".markdown", ".txt"):
            continue
        try:
            sub = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for ref in extract_references(sub):
            rel = _resolve_ref(root, f.parent, ref)
            if rel:
                referenced.add(rel)

    def covered(rel: str) -> bool:
        parts = rel.split("/")
        return any("/".join(parts[:i]) in referenced for i in range(1, len(parts) + 1))

    unreferenced, empty, long_no_toc = [], [], []
    for d in RESOURCE_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        files = [(p, rel) for p, rel, is_dir in _walk(base) if not is_dir]
        if not files:
            empty.append(d + "/")
        for p, rel in files:
            rel_s = f"{d}/" + "/".join(rel)
            if not covered(rel_s):
                unreferenced.append(rel_s)
            if d == "references" and p.suffix.lower() == ".md":
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                if len(lines) > MAX_REFERENCE_LINES and not _has_toc(lines):
                    long_no_toc.append(rel_s)
    c.check("unreferenced_files", not unreferenced,
            "File(s) never referenced from SKILL.md: " + ", ".join(unreferenced), "warnings")
    c.check("empty_resource_dirs", not empty, "Empty resource directories: " + ", ".join(empty), "warnings")
    c.check("reference_toc", not long_no_toc,
            f"Reference file(s) over {MAX_REFERENCE_LINES} lines with no table of contents: "
            + ", ".join(long_no_toc), "warnings")
    aux = [n for n in AUXILIARY_DOCS if (root / n).exists()]
    c.check("auxiliary_docs", not aux, "Auxiliary doc(s) present: " + ", ".join(aux), "warnings")

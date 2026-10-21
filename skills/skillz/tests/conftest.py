"""Shared pytest setup: make skillz scripts importable by module name."""

import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

for p in (SKILL_ROOT / "scripts", SKILL_ROOT / "scripts" / "eval"):
    sys.path.insert(0, str(p))


import json
import os

import pytest

FAKE_CLAUDE_DIR = Path(__file__).resolve().parent / "fake_claude"


class FakeClaude:
    """Controller for the fake `claude` shim (see fake_claude/claude)."""

    def __init__(self, root: Path):
        self.root = root
        self.script = root / "script.json"
        self.log = root / "calls.jsonl"
        self.state = root / "state"
        self.set_rules([])

    def set_rules(self, rules, version="2.1.300 (Claude Code)"):
        """rules: [{"match": substr|None, "responses": [resp, ...]}]."""
        self.script.write_text(json.dumps({"version": version, "rules": rules}))

    def respond(self, *responses, match=None):
        """Single-rule shorthand: every call consumes the next response."""
        self.set_rules([{"match": match, "responses": list(responses)}])

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    root = tmp_path / "fake_claude_state"
    root.mkdir()
    monkeypatch.setenv("PATH", f"{FAKE_CLAUDE_DIR}{os.pathsep}{os.environ['PATH']}")
    ctl = FakeClaude(root)
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(ctl.script))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(ctl.log))
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(ctl.state))
    return ctl


@pytest.fixture
def tmp_area(tmp_path, monkeypatch):
    """Point tempfile.gettempdir() (and child processes' TMPDIR) at a private dir."""
    area = tmp_path / "tmp_area"
    area.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(area))
    monkeypatch.setenv("TMPDIR", str(area))
    return area

"""run_eval: trigger trial engine (criteria 26, 27, 29-34, 51, 54)."""

import json
import os
import types
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import claude_cli as cc
import run_eval as re_
import validation

NAME = "pdf-helper"
SKILL_MD = (
    "---\n"
    f"name: {NAME}\n"
    "description: Old description about PDFs.\n"
    "when_to_use: When the user mentions forms.\n"
    "paths: ['**/*.pdf']\n"
    "---\n\n"
    "# PDF helper\n\nBody text.  \n\tTabbed line\n"
)
CANARY_TOKEN = "abc1234567"
CANARY_NAME = f"skillz-canary-{CANARY_TOKEN}"

TRIG = {"skill_call": NAME}
NOT = {}
ERR = {"exit_code": 1, "is_error": True, "stderr": "boom"}


@pytest.fixture(autouse=True)
def fixed_canary(monkeypatch):
    monkeypatch.setattr(re_.uuid, "uuid4", lambda: types.SimpleNamespace(hex=CANARY_TOKEN + "f" * 22))


@pytest.fixture
def skill(tmp_path, tmp_area):
    d = tmp_path / "skill" / NAME
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL_MD)
    (d / "scripts").mkdir()
    (d / "scripts" / "x.py").write_text("print(1)\n")
    return d


def items(*specs):
    return [{"id": f"q{i}", "query": q, "should_trigger": st} for i, (q, st) in enumerate(specs, 1)]


def evaluate(eval_set, skill, **kw):
    kw.setdefault("num_workers", 1)
    kw.setdefault("runs_per_query", 3)
    return re_.evaluate_description(
        eval_set, skill_name=NAME, skill_md=SKILL_MD, description="New description.",
        model="m", skill_dir=skill, **kw)


def rules(fake, **per_query):
    fake.set_rules([{"match": q, "responses": r} for q, r in per_query.items()])


# ------------------------------------------------------------ c54 adaptive

def test_c54_agreeing_first_two_get_no_third_run(fake_claude, skill):
    rules(fake_claude, alpha=[TRIG, TRIG, NOT])
    res = evaluate(items(("alpha", True)), skill)
    q = res.per_query[0]
    assert q["runs_used"] == 2 and len(fake_claude.calls()) == 2
    assert q["trigger_rate"] == 1.0 and q["result"] == "pass"


def test_c54_disagreeing_first_two_get_exactly_one_more(fake_claude, skill):
    rules(fake_claude, alpha=[TRIG, NOT, TRIG, NOT, NOT])
    res = evaluate(items(("alpha", True)), skill)
    q = res.per_query[0]
    assert q["runs_used"] == 3 and len(fake_claude.calls()) == 3
    assert q["trigger_rate"] == pytest.approx(2 / 3) and q["result"] == "pass"


def test_c54_error_run_does_not_count_toward_agreement(fake_claude, skill):
    # trig, ERR: only one non-error outcome, so a run is still needed; the
    # two non-error runs then agree, so no 4th run.
    rules(fake_claude, alpha=[TRIG, ERR, TRIG, NOT])
    q = evaluate(items(("alpha", True)), skill, runs_per_query=5).per_query[0]
    assert [o["outcome"] for o in q["outcomes"]] == ["triggered", "error", "triggered"]
    assert q["runs_used"] == 3 and q["result"] == "pass"


def test_c54_error_between_disagreeing_runs_still_one_tiebreak(fake_claude, skill):
    rules(fake_claude, alpha=[TRIG, NOT, ERR, TRIG, TRIG])
    q = evaluate(items(("alpha", True)), skill, runs_per_query=5).per_query[0]
    assert [o["outcome"] for o in q["outcomes"]] == ["triggered", "not_triggered", "error", "triggered"]
    assert q["trigger_rate"] == pytest.approx(2 / 3)


def test_c54_fewer_than_two_non_error_outcomes_is_error(fake_claude, skill):
    rules(fake_claude, alpha=[TRIG, ERR, ERR])
    res = evaluate(items(("alpha", True)), skill)
    q = res.per_query[0]
    assert q["runs_used"] == 3 and q["result"] == "error"
    assert res.summary == {"total": 1, "passed": 0, "failed": 0, "errors": 1}
    assert res.error_runs == 2 and res.error_queries == 1


def test_c54_max_runs_caps_disagreement(fake_claude, skill):
    rules(fake_claude, alpha=[TRIG, NOT, TRIG])
    q = evaluate(items(("alpha", True)), skill, runs_per_query=2).per_query[0]
    assert q["runs_used"] == 2 and q["trigger_rate"] == 0.5


def test_c54_fixed_runs_always_uses_n(fake_claude, skill):
    rules(fake_claude, alpha=[TRIG])
    q = evaluate(items(("alpha", True)), skill, runs_per_query=4, fixed_runs=True).per_query[0]
    assert q["runs_used"] == 4 and len(fake_claude.calls()) == 4


def test_c54_threshold_decides_pass_for_negative_queries(fake_claude, skill):
    rules(fake_claude, alpha=[TRIG, NOT, TRIG])
    q = evaluate(items(("alpha", False)), skill).per_query[0]
    assert q["trigger_rate"] == pytest.approx(2 / 3) and q["result"] == "fail"


# --------------------------------------------------------- c27 grouping

def test_c27_duplicate_query_text_is_scored_independently(fake_claude, skill):
    rules(fake_claude, same=[TRIG, TRIG, NOT, NOT])
    es = items(("same", True), ("same", False))
    res = evaluate(es, skill)
    assert [q["id"] for q in res.per_query] == ["q1", "q2"]
    assert res.per_query[0]["trigger_rate"] == 1.0 and res.per_query[0]["result"] == "pass"
    assert res.per_query[1]["trigger_rate"] == 0.0 and res.per_query[1]["result"] == "pass"


def test_c27_load_eval_set_assigns_distinct_ids_skipping_taken(tmp_path):
    p = tmp_path / "e.json"
    p.write_text(json.dumps([
        {"query": "a", "should_trigger": True},
        {"id": "q3", "query": "a", "should_trigger": False},
        {"query": "a", "should_trigger": True},
    ]))
    ids = [i["id"] for i in re_.load_eval_set(p)]
    assert ids == ["q1", "q3", "q2"] and len(set(ids)) == 3


def test_c27_load_eval_set_rejects_duplicate_ids_and_bad_entries(tmp_path):
    p = tmp_path / "e.json"
    p.write_text(json.dumps([{"id": "x", "query": "a", "should_trigger": True},
                             {"id": "x", "query": "b", "should_trigger": True}]))
    with pytest.raises(ValueError, match="duplicate id"):
        re_.load_eval_set(p)
    p.write_text(json.dumps([{"query": "a"}]))
    with pytest.raises(ValueError, match="should_trigger"):
        re_.load_eval_set(p)


# ------------------------------------------------- c26 infrastructure error

@pytest.mark.parametrize("resp,reason", [
    ({"exit_code": 1, "is_error": True, "stderr": "model not found: bad-model"}, "exit code 1"),
    ({"exit_code": 137, "no_result": True}, "exit code 137"),
    ({"denial": "Write"}, "permission denied"),
])
def test_c26_infrastructure_failure_is_error_not_non_trigger(fake_claude, skill, resp, reason):
    rules(fake_claude, alpha=[resp])
    # should_trigger False: treating the failure as a non-trigger would "pass".
    res = evaluate(items(("alpha", False)), skill)
    q = res.per_query[0]
    assert q["result"] == "error" and q["trigger_rate"] is None
    assert all(o["outcome"] == "error" for o in q["outcomes"])
    assert reason in q["outcomes"][0]["reason"]
    assert res.summary["passed"] == 0 and res.summary["errors"] == 1
    assert res.error_runs == q["runs_used"] >= 2


def test_c26_errors_do_not_dilute_the_trigger_rate(fake_claude, skill):
    rules(fake_claude, alpha=[ERR, TRIG, TRIG])
    q = evaluate(items(("alpha", True)), skill).per_query[0]
    assert q["trigger_rate"] == 1.0


def test_c26_error_outcome_records_exit_code_and_stderr(fake_claude, skill):
    rules(fake_claude, alpha=[ERR])
    o = evaluate(items(("alpha", True)), skill).per_query[0]["outcomes"][0]
    assert o["exit_code"] == 1 and "boom" in o["stderr_tail"]


# ------------------------------------------------------------- c29 stream

def test_c29_other_tool_before_skill_call_is_a_trigger(fake_claude, skill):
    rules(fake_claude, alpha=[{"tools_before": ["Glob"], "skill_call": NAME}])
    q = evaluate(items(("alpha", True)), skill).per_query[0]
    assert q["trigger_rate"] == 1.0 and q["result"] == "pass"


def test_c29_substring_skill_name_does_not_count(fake_claude, skill):
    rules(fake_claude, alpha=[{"skill_call": NAME + "-other"}])
    assert evaluate(items(("alpha", True)), skill).per_query[0]["trigger_rate"] == 0.0


# ------------------------------------------------------------ c30 canary

def run_cli(args, **kw):
    """Click 8.2+: r.stdout is stdout only, r.stderr is stderr, r.output is both."""
    return CliRunner().invoke(re_.main, args, **kw)


@pytest.fixture
def cli_files(tmp_path, skill):
    ev = tmp_path / "eval.json"
    ev.write_text(json.dumps([{"query": "alpha", "should_trigger": True},
                              {"query": "beta", "should_trigger": False}]))
    return ["--eval-set", str(ev), "--skill-path", str(skill), "--model", "m"]


@pytest.mark.parametrize("canary_resp", [
    {},  # model answers but never invokes the canary skill
    {"exit_code": 1, "is_error": True, "stderr": "auth required"},
    {"exit_code": 1, "stderr": "nested session refused", "no_result": True},
])
def test_c30_failed_canary_aborts_before_any_real_query(fake_claude, cli_files, canary_resp):
    fake_claude.set_rules([{"match": "code word", "responses": [canary_resp]},
                           {"match": None, "responses": [TRIG]}], version="9.9.9 (Claude Code)")
    r = run_cli([*cli_files, "--yes"])
    assert r.exit_code == 3
    out = r.output
    assert "CLI version: 2.1.300" in out
    assert f"exit code: {canary_resp.get('exit_code', 0)}" in out
    if "stderr" in canary_resp:
        assert canary_resp["stderr"] in out
    calls = fake_claude.calls()
    assert len(calls) == 1 and "code word" in calls[0]["argv"][1]


def test_c30_version_falls_back_to_claude_version_when_stream_has_no_init(fake_claude, cli_files, tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    fake_claude.set_rules([{"match": None, "responses": [
        {"replay": str(empty), "exit_code": 1, "stderr": "dead"}]}], version="9.9.9 (Claude Code)")
    r = run_cli([*cli_files, "--yes"])
    assert r.exit_code == 3 and "CLI version: 9.9.9" in r.output and "dead" in r.output


def test_c30_canary_requires_exact_match(fake_claude, skill):
    fake_claude.respond({"skill_call": CANARY_NAME + "x"})
    with pytest.raises(re_.CanaryFailed):
        re_.preflight_canary("m")


def test_c30_passing_canary_is_counted_in_budget(fake_claude, skill):
    fake_claude.respond({"skill_call": CANARY_NAME, "cost": 0.25})
    budget = cc.Budget(10)
    res = re_.preflight_canary("m", budget=budget)
    assert res.call["kind"] == "canary" and budget.spent == pytest.approx(0.25)
    assert res.version == "2.1.300"


def test_c30_budget_cap_crossed_by_canary_stops_real_queries(fake_claude, skill):
    fake_claude.respond({"skill_call": CANARY_NAME, "cost": 1.0})
    budget = cc.Budget(0.5)
    re_.preflight_canary("m", budget=budget)
    res = evaluate(items(("alpha", True)), skill, budget=budget)
    assert res.budget_stopped and res.per_query[0]["result"] == "error"
    assert len(fake_claude.calls()) == 1


# ------------------------------------------------------------- c31 / c32

@pytest.fixture
def spy(monkeypatch):
    """Record each trial project's skills dir and SKILL.md as the call starts."""
    seen = []
    real = cc.run_and_parse

    def wrapper(call, cwd, **kw):
        skills = Path(cwd) / ".claude" / "skills"
        seen.append({
            "cwd": str(cwd),
            "skills": sorted(p.name for p in skills.iterdir()),
            "md": (skills / NAME / "SKILL.md").read_text() if (skills / NAME).exists() else None,
            "extra_files": sorted(str(p.relative_to(skills / NAME)) for p in (skills / NAME).rglob("*") if p.is_file())
            if (skills / NAME).exists() else [],
        })
        return real(call, cwd, **kw)

    monkeypatch.setattr(cc, "run_and_parse", wrapper)
    return seen


def test_c31_concurrent_trials_use_separate_projects(fake_claude, skill, spy):
    fake_claude.respond({"skill_call": NAME, "sleep": 0.3})
    es = items(*[(f"query {i}", True) for i in range(6)])
    res = evaluate(es, skill, num_workers=6, runs_per_query=2)
    assert len(spy) == 12
    assert len({s["cwd"] for s in spy}) == 12
    assert all(s["skills"] == [NAME] for s in spy)
    assert len({c["cwd"] for c in fake_claude.calls()}) == 12
    assert all(not Path(s["cwd"]).exists() for s in spy)  # cleaned up
    assert res.summary["passed"] == 6


def test_c32_staged_skill_keeps_full_frontmatter_with_only_description_changed(fake_claude, skill, spy):
    fake_claude.respond(TRIG)
    evaluate(items(("alpha", True)), skill)
    staged = spy[0]["md"]
    fm, body, err = validation.parse_frontmatter(staged)
    orig_fm, orig_body, _ = validation.parse_frontmatter(SKILL_MD)
    assert err is None
    assert fm["description"] == "New description."
    assert {k: v for k, v in fm.items() if k != "description"} == \
           {k: v for k, v in orig_fm.items() if k != "description"}
    assert list(fm) == list(orig_fm) and body == orig_body
    assert "scripts/x.py" in spy[0]["extra_files"]  # whole skill dir staged


@pytest.mark.parametrize("original", [
    "---\nname: a\ndescription: >\n  folded text\n  more\nwhen_to_use: x\n---\nBody\n",
    "---\nname: a\ndescription: 'quoted: yes'\nlicense: MIT\n---\n\nBody\n",
    "---\nname: a\nwhen_to_use: x\ndescription: |\n  literal\n  lines\n\n---\nBody\n",
    "---\r\nname: a\r\ndescription: old\r\nmetadata:\r\n  k: v\r\n---\r\nBody\r\n",
    "﻿---\nname: a\ndescription: old\n---\nBody\n",
    "---\nname: a\ndescription: old # not a comment? it is\nzed: 1\n---\n",
])
def test_c32_with_description_preserves_everything_else(original):
    new = 'Handles "quotes", colons: and\nnewlines # hash é \\ backslash'
    out = re_.with_description(original, new)
    fm_o, body_o, _ = validation.parse_frontmatter(original)
    fm_n, body_n, err = validation.parse_frontmatter(out)
    assert err is None and fm_n["description"] == new
    assert list(fm_n) == list(fm_o) and body_n == body_o
    assert {k: v for k, v in fm_n.items() if k != "description"} == \
           {k: v for k, v in fm_o.items() if k != "description"}
    # untouched lines are byte-identical
    keep = [l for l in original.splitlines(keepends=True) if l.startswith(("name:", "license:", "when_to_use:", "zed:"))]
    assert all(l in out for l in keep)


def test_c32_with_description_body_is_byte_identical():
    out = re_.with_description(SKILL_MD, "New")
    assert out.endswith(SKILL_MD.split("---\n", 2)[2])
    assert re_.with_description(SKILL_MD, "Old description about PDFs.").count("\n") == SKILL_MD.count("\n")


def test_c32_with_description_requires_a_description():
    with pytest.raises(ValueError, match="no top-level `description`"):
        re_.with_description("---\nname: a\n---\nBody\n", "x")


# ----------------------------------------------------------------- c33

def test_c33_disable_model_invocation_is_refused_with_zero_calls(fake_claude, tmp_path):
    d = tmp_path / "manual"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: manual\ndescription: d\ndisable-model-invocation: true\n---\nB\n")
    ev = tmp_path / "e.json"
    ev.write_text(json.dumps([{"query": "q", "should_trigger": True}]))
    r = run_cli(["--eval-set", str(ev), "--skill-path", str(d), "--model", "m", "--yes"])
    assert r.exit_code != 0 and "disable-model-invocation" in r.output
    assert fake_claude.calls() == [] and not fake_claude.state.exists()


def test_c33_library_refusal():
    with pytest.raises(re_.RefusedSkill, match="auto-triggers"):
        re_.check_runnable("---\nname: a\ndescription: d\ndisable-model-invocation: true\n---\n")
    re_.check_runnable("---\nname: a\ndescription: d\ndisable-model-invocation: false\n---\n")


# ----------------------------------------------------------------- c34

def test_c34_trial_argv_is_built_by_claude_cli_with_required_flags(fake_claude, skill):
    rules(fake_claude, alpha=[TRIG, TRIG])
    res = evaluate(items(("alpha", True)), skill, extra_args=["--some-future-flag", "v"])
    argv = fake_claude.calls()[0]["argv"]
    assert argv[:2] == ["-p", "alpha"]
    for f in ("--no-session-persistence", "--strict-mcp-config"):
        assert f in argv
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert "Skill" in argv[argv.index("--tools") + 1].split(",")
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[-2:] == ["--some-future-flag", "v"]  # passthrough last, verbatim
    rec = res.calls[0]
    assert rec["extra_args"] == ["--some-future-flag", "v"] and rec["argv"][1] == "-p"
    assert rec["exit_code"] == 0 and rec["kind"] == "trigger" and rec["query_id"] == "q1"


def test_c34_script_builds_no_argv_itself():
    src = Path(re_.__file__).read_text()
    assert "subprocess" not in src and '"--output-format"' not in src


def test_c34_bad_passthrough_is_rejected_before_any_call(fake_claude, skill, cli_files):
    r = run_cli([*cli_files, "--yes", "--", "--setting-sources", "user"])
    assert r.exit_code != 0 and "--setting-sources" in r.output
    assert fake_claude.calls() == []


# ----------------------------------------------------------------- c51

def test_c51_without_yes_prints_plan_and_makes_zero_calls(fake_claude, cli_files):
    r = run_cli(cli_files)
    assert r.exit_code == 0 and fake_claude.calls() == []
    assert "worst case: 7" in r.output and "canary" in r.output  # 2 queries x 3 + canary
    assert "--yes" in r.output


def test_c51_plan_json_and_fixed_runs(fake_claude, cli_files):
    r = run_cli([*cli_files, "--runs-per-query", "4", "--fixed-runs", "--json"])
    plan = json.loads(r.stdout)["plan"]
    assert plan == {"canary": 1, "worst_case": 9, "expected_min": 9}
    assert fake_claude.calls() == []


# --------------------------------------------------------- CLI end to end

def test_cli_yes_runs_canary_then_queries_and_reports(fake_claude, cli_files, tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(
        {"skillListingMaxDescChars": 900, "skillOverrides": {NAME: "name-only"}}))
    os.environ["HOME"] = str(tmp_path)
    fake_claude.set_rules([
        {"match": "code word", "responses": [{"skill_call": CANARY_NAME, "cost": 0.5}]},
        {"match": "alpha", "responses": [TRIG]},
        {"match": "beta", "responses": [NOT]},
    ])
    r = run_cli([*cli_files, "--yes", "--json", "--description", "Brand new.", "--", "--some-future-flag", "v"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["summary"] == {"total": 2, "passed": 2, "failed": 0, "errors": 0}
    assert [q["id"] for q in out["results"]] == ["q1", "q2"]
    assert out["calls"] == {"planned_worst_case": 7, "made": 5, "canary": 1}
    assert out["call_log"][0]["kind"] == "canary"
    assert all(c["extra_args"] == ["--some-future-flag", "v"] for c in out["call_log"])
    ls = out["listing_settings"]
    assert ls["skillListingMaxDescChars"] == 900 and ls["skillOverrides"] == "name-only"
    assert ls["when_to_use_chars"] == len("When the user mentions forms.") and ls["description_chars"] == 10
    assert "default" in ls["trial_listing"]
    assert out["error_counts"] == {"error_runs": 0, "error_queries": 0}
    assert out["cli_version"] == "2.1.300" and out["total_cost_usd"] == pytest.approx(0.504)
    assert len(fake_claude.calls()) == 5  # canary + 2 queries x 2 runs


def test_collect_listing_settings_env_and_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SLASH_COMMAND_TOOL_CHAR_BUDGET", "8000")
    s = re_.collect_listing_settings({"name": "a", "description": "abc"})
    assert s["SLASH_COMMAND_TOOL_CHAR_BUDGET"] == "8000"
    assert s["skillListingBudgetFraction"] is None and s["skillOverrides"] is None
    assert s["description_chars"] == 3 and s["when_to_use_chars"] == 0

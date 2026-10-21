"""run_loop / improve_description / generate_report (criteria 25, 27, 28, 32, 33, 50, 51, 53, 55, 56)."""

import hashlib
import json
import os
import types
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import claude_cli as cc
import generate_report as gr
import improve_description as imp
import run_eval
import run_loop as rl
import validation

NAME = "pdf-helper"
SKILL_MD = (
    "---\n"
    f"name: {NAME}\n"
    "description: Old description about PDFs.\n"
    "when_to_use: When the user mentions forms.\n"
    "paths: ['**/*.pdf']\n"
    "---\n\n"
    "# PDF helper\n\nBody text.\n"
)
CANARY_TOKEN = "abc1234567"
CANARY_NAME = f"skillz-canary-{CANARY_TOKEN}"
REWRITE_MATCH = "optimizing a skill description"

POS = [f"pos{i} please" for i in range(1, 6)]
NEG = [f"neg{i} please" for i in range(1, 6)]


def rewrite_resp(desc, cost=0.001):
    return {"text": f"<new_description>{desc}</new_description>", "cost": cost}


def verdict(seq, cost=0.001):
    """Each verdict ("T"/"N") stands for one adaptive evaluation: two agreeing runs."""
    out = []
    for v in seq:
        r = {"skill_call": NAME} if v == "T" else {}
        out += [{**r, "cost": cost}, {**r, "cost": cost}]
    return out


def script(fake, verdicts, rewrites=(), canary_cost=0.001, trig_cost=0.001, rewrite_cost=0.001):
    rules = [{"match": "The code word is", "responses": [{"skill_call": CANARY_NAME, "cost": canary_cost}]}]
    if rewrites:
        rules.append({"match": REWRITE_MATCH, "responses": [rewrite_resp(d, rewrite_cost) for d in rewrites]})
    for q, seq in verdicts.items():
        rules.append({"match": q, "responses": verdict(seq, trig_cost)})
    fake.set_rules(rules)


@pytest.fixture(autouse=True)
def fixed_canary(monkeypatch):
    monkeypatch.setattr(run_eval.uuid, "uuid4", lambda: types.SimpleNamespace(hex=CANARY_TOKEN + "f" * 22))


@pytest.fixture
def env(tmp_path, tmp_area, monkeypatch, fake_claude):
    """A skill, an eval set, a fake HOME and a project, all under tmp_path."""
    skill = tmp_path / "skills-src" / NAME
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL_MD)
    (skill / "scripts" / "x.py").write_text("print(1)\n")

    home = tmp_path / "home"
    (home / ".claude" / "skills" / "mine").mkdir(parents=True)
    (home / ".claude" / "skills" / "mine" / "SKILL.md").write_text("---\nname: mine\ndescription: d\n---\n")
    (home / ".claude" / "commands").mkdir()
    (home / ".claude" / "commands" / "c.md").write_text("cmd\n")
    (home / ".claude" / "settings.json").write_text(json.dumps({"skillListingMaxDescChars": 1536}))

    project = tmp_path / "project"
    (project / ".claude" / "commands").mkdir(parents=True)
    (project / ".claude" / "commands" / "p.md").write_text("pcmd\n")
    (project / ".claude" / "settings.local.json").write_text("{}")

    items = [{"query": q, "should_trigger": True} for q in POS] + \
            [{"query": q, "should_trigger": False} for q in NEG]
    evalset = tmp_path / "evalset.json"
    evalset.write_text(json.dumps(items))

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    return types.SimpleNamespace(skill=skill, home=home, project=project, evalset=evalset,
                                 tmp_area=tmp_area, items=run_eval.load_eval_set(evalset), fake=fake_claude)


def run(env, *extra, yes=True, expect_ok=True):
    args = ["--eval-set", str(env.evalset), "--skill-path", str(env.skill), "--model", "m",
            "--num-workers", "1", "--json"]
    if yes:
        args.append("--yes")
    res = CliRunner().invoke(rl.main, [*args, *extra])
    if expect_ok:
        assert res.exit_code == 0, res.output
    return res


def split(env):
    train, test = rl.split_eval_set(env.items, 0.4)
    pick = lambda xs, pol: [e["query"] for e in xs if e["should_trigger"] is pol]  # noqa: E731
    return (types.SimpleNamespace(pos=pick(train, True), neg=pick(train, False)),
            types.SimpleNamespace(pos=pick(test, True), neg=pick(test, False)))


def tree_hash(*roots):
    h = hashlib.sha256()
    for root in roots:
        for p in sorted(Path(root).rglob("*")):
            h.update(str(p.relative_to(root)).encode())
            if p.is_file():
                h.update(p.read_bytes())
    return h.hexdigest()


def protected(env):
    return tree_hash(env.home / ".claude", env.project / ".claude", env.skill)


def only_results_dir(env):
    dirs = list((env.project / f"{NAME}-workspace" / "trigger-opt").iterdir())
    assert len(dirs) == 1
    return dirs[0]


def regress_script(env):
    """Iter 1 train 4/6 test 3/4; iter 2 train 2/6 (test skipped); iter 3 train 6/6, test 4/4."""
    tr, te = split(env)
    v = {
        tr.pos[0]: ["N", "N", "T"], tr.pos[1]: ["N", "N", "T"], tr.pos[2]: ["T", "N", "T"],
        tr.neg[0]: ["N", "T", "N"], tr.neg[1]: ["N", "N", "N"], tr.neg[2]: ["N", "N", "N"],
        te.pos[0]: ["N", "T"], te.pos[1]: ["T", "T"], te.neg[0]: ["N", "N"], te.neg[1]: ["N", "N"],
    }
    script(env.fake, v, rewrites=["Second candidate description.", "Third candidate description."])
    return tr, te


# ------------------------------------------------------------ c27 split

def test_c27_duplicate_queries_split_by_id():
    items = [{"id": f"q{i}", "query": "same text", "should_trigger": i % 2 == 0} for i in range(10)]
    train, test = rl.split_eval_set(items, 0.4)
    assert len(train) == 6 and len(test) == 4
    assert {e["id"] for e in train}.isdisjoint({e["id"] for e in test})
    assert sorted(e["id"] for e in train + test) == sorted(e["id"] for e in items)
    assert rl.split_eval_set(items, 0.4) == (train, test)  # seeded, repeatable


def test_c27_duplicates_scored_independently_through_the_loop(env):
    dup = [{"query": "same text", "should_trigger": i % 2 == 0} for i in range(10)]
    env.evalset.write_text(json.dumps(dup))
    script(env.fake, {"same text": ["T"]})
    out = json.loads(run(env, "--holdout", "0.4", "--max-iterations", "1").output)
    it = out["iterations"][0]
    ids = [q["id"] for q in it["train"]["per_query"]] + [q["id"] for q in it["test"]["per_query"]]
    assert len(ids) == 10 and len(set(ids)) == 10


def test_holdout_zero_has_no_test_set(env):
    script(env.fake, {})
    out = json.loads(run(env, "--holdout", "0", "--max-iterations", "1").output)
    assert out["iterations"][0]["test"] is None
    assert out["iterations"][0]["train"]["total"] == 10
    assert out["best_selection"]["method"].startswith("highest train score")
    assert out["best_selection"]["test_score"] is None


# ------------------------------------------------------------ c28 / c51 / c56 plan

def test_c28_c51_c56_plan_printed_and_zero_calls_without_yes(env):
    res = run(env, yes=False, expect_ok=True)
    plan = json.loads(res.output)
    # 10 queries x 3 runs x 3 iterations + 1 canary + 2 rewrites
    assert plan["plan"]["worst_case"] == 93
    assert (plan["plan"]["expected_min"], plan["plan"]["expected_max"]) == (47, 71)
    assert plan["calls_made"] == 0 and plan["confirmed"] is False
    assert env.fake.calls() == []
    assert not (env.project / f"{NAME}-workspace").exists()


def test_c28_plain_plan_text_states_worst_case_and_range(env):
    res = CliRunner().invoke(rl.main, ["--eval-set", str(env.evalset), "--skill-path", str(env.skill),
                                       "--model", "m"])
    assert res.exit_code == 0
    assert "= 93 claude -p calls" in res.output and "3 iterations" in res.output
    assert "1 canary + 2 rewrites" in res.output and "47-71" in res.output
    assert "No calls were made" in res.output
    assert env.fake.calls() == []


def test_c56_default_is_three_iterations(env):
    # Positives never trigger, so train never fully passes and the loop runs to its limit.
    script(env.fake, {}, rewrites=["Rewrite one.", "Rewrite two.", "Rewrite three."])
    out = json.loads(run(env).output)
    assert len(out["iterations"]) == 3 and out["settings"]["max_iterations"] == 3
    assert out["exit_reason"] == "max_iterations (3)"
    assert out["calls"]["rewrites"] == 2 and out["calls"]["made"] <= out["calls"]["planned_worst_case"]


def test_c56_fixed_runs_worst_case_equals_expected_max(env):
    plan = json.loads(run(env, "--fixed-runs", yes=False).output)["plan"]
    assert plan["worst_case"] == 93 and plan["expected_max"] == 93


# ------------------------------------------------------------ c33 refusal

def test_c33_manual_only_skill_refused_before_any_call(env):
    (env.skill / "SKILL.md").write_text(SKILL_MD.replace("paths:", "disable-model-invocation: true\npaths:"))
    res = run(env, expect_ok=False)
    assert res.exit_code != 0 and "disable-model-invocation" in res.output
    assert env.fake.calls() == []
    assert not (env.project / f"{NAME}-workspace").exists()


# ------------------------------------------------------------ c55 gating

def test_c55_regressed_candidate_skips_test_and_is_never_selected(env):
    tr, te = regress_script(env)
    out = json.loads(run(env).output)
    its = out["iterations"]
    assert [i["train"]["passed"] for i in its] == [4, 2, 6]
    assert its[0]["test"]["passed"] == 3
    assert its[1]["test"] == "skipped (train regressed)"
    test_ids = {e["id"] for e in env.items if e["query"] in te.pos + te.neg}
    assert not [c for c in its[1]["calls"] if c.get("query_id") in test_ids]
    assert its[2]["test"]["passed"] == 4
    assert out["best_selection"]["iteration"] == 3
    assert out["best_description"] == "Third candidate description."
    assert out["best_selection"]["train_score"] == "6/6" and out["best_selection"]["test_score"] == "4/4"
    assert out["exit_reason"] == "all train queries passed"


def test_c55_skipped_candidate_loses_even_when_it_would_have_tied(env):
    # Iteration 3 never improves; if iteration 2's test had been run it would score 4/4 and win.
    tr, te = split(env)
    script(env.fake, {
        tr.pos[0]: ["N", "N", "N"], tr.pos[1]: ["T", "N", "T"], tr.pos[2]: ["T", "N", "T"],
        tr.neg[0]: ["N", "N", "N"], tr.neg[1]: ["N", "N", "N"], tr.neg[2]: ["N", "N", "N"],
        te.pos[0]: ["N", "T"], te.pos[1]: ["N", "T"], te.neg[0]: ["N", "N"], te.neg[1]: ["N", "N"],
    }, rewrites=["Second.", "Third."])
    out = json.loads(run(env).output)
    its = out["iterations"]
    assert its[1]["test"] == "skipped (train regressed)"
    assert out["best_selection"]["iteration"] != 2
    assert out["best_description"] != "Second."


def test_rewrites_see_train_failures_only(env):
    tr, te = regress_script(env)
    run(env)
    prompts = [c["argv"][1] for c in env.fake.calls() if REWRITE_MATCH in c["argv"][1]]
    assert len(prompts) == 2
    for p in prompts:
        for q in te.pos + te.neg:
            assert q not in p
    assert tr.pos[0] in prompts[0]  # a train failure is shown


# ------------------------------------------------------------ c32 frontmatter

def test_c32_with_description_preserves_every_other_field():
    out = run_eval.with_description(SKILL_MD, "Brand new description.")
    old, _, _ = validation.parse_frontmatter(SKILL_MD)
    new, body, err = validation.parse_frontmatter(out)
    assert err is None
    assert new["description"] == "Brand new description."
    assert {k: v for k, v in new.items() if k != "description"} == {k: v for k, v in old.items() if k != "description"}
    assert list(new) == list(old) and body == "\n# PDF helper\n\nBody text.\n"


def test_c32_staged_skill_md_carries_full_frontmatter_each_trial(env, monkeypatch):
    staged = []
    real = cc.stage_skill

    def spy(project, name, **kw):
        staged.append(kw.get("skill_md"))
        return real(project, name, **kw)

    monkeypatch.setattr(cc, "stage_skill", spy)
    regress_script(env)
    run(env, "--max-iterations", "2")
    trials = [s for s in staged if s and CANARY_NAME not in s]
    assert trials
    seen = set()
    for md in trials:
        fm, body, err = validation.parse_frontmatter(md)
        assert err is None
        assert fm["when_to_use"] == "When the user mentions forms." and fm["paths"] == ["**/*.pdf"]
        assert fm["name"] == NAME and body == "\n# PDF helper\n\nBody text.\n"
        seen.add(fm["description"])
    assert seen == {"Old description about PDFs.", "Second candidate description."}


# ------------------------------------------------------------ c50 budget

def test_c50_max_total_usd_stops_launching_and_marks_incomplete(env):
    script(env.fake, {q: ["T"] for q in POS + NEG}, canary_cost=0.01, trig_cost=0.01)
    out = json.loads(run(env, "--max-total-usd", "0.025").output)
    assert len(env.fake.calls()) == 3  # canary + 2 runs; the cap was crossed at 0.03
    assert out["exit_reason"] == "max_total_usd reached"
    assert out["iterations"][0]["incomplete"] is True
    assert out["best_description"] is None
    assert out["total_cost_usd"] == pytest.approx(0.03)


def test_c50_cap_covers_rewrite_calls(env):
    tr, te = split(env)
    verdicts = {q: ["N"] for q in POS} | {q: ["N"] for q in NEG}
    script(env.fake, verdicts, rewrites=["Second."], trig_cost=0.0, canary_cost=0.0, rewrite_cost=0.05)
    out = json.loads(run(env, "--max-total-usd", "0.05").output)
    assert out["calls"]["rewrites"] == 1
    assert out["exit_reason"] == "max_total_usd reached"
    assert out["total_cost_usd"] == pytest.approx(0.05)
    assert out["iterations"][-1]["incomplete"] is True and out["iterations"][-1]["description"] == "Second."
    assert out["best_selection"]["iteration"] == 1


# ------------------------------------------------------------ c53 results location

def test_c53_default_results_location_and_no_temp_leak(env):
    regress_script(env)
    run(env)
    d = only_results_dir(env)
    assert (d / "results.json").is_file() and (d / "report.html").is_file() and (d / "logs").is_dir()
    assert os.listdir(env.tmp_area) == []
    data = json.loads((d / "results.json").read_text())
    assert data["skill_name"] == NAME and data["settings"]["holdout"] == 0.4
    page = (d / "report.html").read_text()
    assert "skipped (train regressed)" in page


def test_c53_results_dir_override(env, tmp_path):
    regress_script(env)
    target = tmp_path / "elsewhere"
    run(env, "--results-dir", str(target))
    assert (target / "results.json").is_file() and (target / "report.html").is_file()
    assert not (env.project / f"{NAME}-workspace").exists()


def test_results_dir_inside_skill_refused_before_calls(env):
    res = run(env, "--results-dir", str(env.skill / "out"), expect_ok=False)
    assert res.exit_code != 0 and "inside the skill directory" in res.output
    assert env.fake.calls() == []


# ------------------------------------------------------------ c25 no writes

def test_c25_user_files_byte_identical_after_full_run(env):
    regress_script(env)
    before = protected(env)
    out = json.loads(run(env).output)
    assert out["best_description"] == "Third candidate description."
    assert protected(env) == before
    assert (env.skill / "SKILL.md").read_text() == SKILL_MD  # reported, never applied
    assert not [p for p in os.listdir(env.tmp_area) if p.startswith("skillz-run-")]


def test_c25_user_files_untouched_after_midrun_crash(env, monkeypatch):
    regress_script(env)
    before = protected(env)

    def boom(*a, **k):
        raise RuntimeError("induced crash")

    monkeypatch.setattr(rl.imp, "improve_description", boom)
    res = run(env, expect_ok=False)
    assert res.exit_code != 0 and isinstance(res.exception, RuntimeError)
    assert protected(env) == before
    assert not [p for p in os.listdir(env.tmp_area) if p.startswith("skillz-run-")]
    data = json.loads((only_results_dir(env) / "results.json").read_text())
    assert data["exit_reason"].startswith("crashed:") and len(data["iterations"]) == 1


# ------------------------------------------------------------ canary / listing / errors

def test_canary_failure_writes_results_and_exits_3(env):
    env.fake.set_rules([{"match": "The code word is", "responses": [{}]}])  # canary not triggered
    res = run(env, expect_ok=False)
    assert res.exit_code == 3 and "Preflight canary failed" in res.output
    assert len(env.fake.calls()) == 1
    data = json.loads((only_results_dir(env) / "results.json").read_text())
    assert data["exit_reason"] == "canary failed" and data["best_description"] is None


def test_listing_cut_is_reported_before_rewrite(env):
    long_when = "x" * 900
    desc = "Use for PDFs. " + "y" * 800
    (env.skill / "SKILL.md").write_text(SKILL_MD.replace("Old description about PDFs.", desc)
                                        .replace("When the user mentions forms.", long_when))
    script(env.fake, {}, rewrites=["Short rewrite."])
    out = json.loads(run(env, "--max-iterations", "2").output)
    assert out["listing_warnings"] and "1536" in out["listing_warnings"][0]
    assert out["listing_settings"]["when_to_use_chars"] == 900
    assert out["listing_settings"]["skillListingMaxDescChars"] == 1536
    assert "default listing" in out["listing_settings"]["trial_listing"] or "default:" in out["listing_settings"]["trial_listing"]
    plain = CliRunner().invoke(rl.main, ["--eval-set", str(env.evalset), "--skill-path", str(env.skill),
                                         "--model", "m", "--num-workers", "1", "--yes", "--max-iterations", "2"])
    assert "WARNING: description + when_to_use" in plain.output


def test_no_listing_warning_when_under_the_cap(env):
    regress_script(env)
    assert json.loads(run(env).output)["listing_warnings"] == []


def test_error_runs_are_counted_not_scored_as_non_triggers(env):
    tr, te = split(env)
    script(env.fake, {q: ["T"] for q in POS} | {q: ["N"] for q in NEG})
    rules = json.loads(env.fake.script.read_text())["rules"]
    rules.insert(1, {"match": tr.pos[0], "responses": [{"exit_code": 1, "is_error": True, "stderr": "boom"}]})
    env.fake.set_rules(rules)
    out = json.loads(run(env, "--max-iterations", "1").output)
    q = next(q for q in out["iterations"][0]["train"]["per_query"] if q["query"] == tr.pos[0])
    assert q["result"] == "error"
    assert out["iterations"][0]["train"]["errors"] == 1
    assert out["error_counts"]["error_queries"] == 1 and out["error_counts"]["error_runs"] >= 2


# ------------------------------------------------------------ improve_description

TRAIN_PQ = [
    {"id": "q1", "query": "make me a pdf form", "should_trigger": True, "result": "fail",
     "outcomes": [{"outcome": "not_triggered"}, {"outcome": "not_triggered"}], "runs_used": 2, "trigger_rate": 0.0},
    {"id": "q2", "query": "write a poem", "should_trigger": False, "result": "pass",
     "outcomes": [{"outcome": "not_triggered"}] * 2, "runs_used": 2, "trigger_rate": 0.0},
    {"id": "q3", "query": "flaky infra query", "should_trigger": True, "result": "error",
     "outcomes": [{"outcome": "error"}], "runs_used": 1, "trigger_rate": None},
]


def improve(fake, answers, skill_md=SKILL_MD, **kw):
    fake.respond(*[{"text": a} for a in answers])
    return imp.improve_description(NAME, skill_md, "Old description about PDFs.", TRAIN_PQ, [], "m", **kw)


def test_rewrite_is_tool_free_isolated_and_passes_through(fake_claude, tmp_area):
    res = improve(fake_claude, ["<new_description>Use for PDF forms.</new_description>"],
                  extra_args=["--some-future-flag", "x"])
    assert res.description == "Use for PDF forms."
    (call,) = fake_claude.calls()
    argv = call["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    for flag in ("--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert argv[-2:] == ["--some-future-flag", "x"]
    assert "skillz-run-" in call["cwd"]
    assert os.listdir(tmp_area) == []


def test_rewrite_prompt_has_when_to_use_and_correct_caps(fake_claude, tmp_area):
    improve(fake_claude, ["<new_description>Use for PDF forms.</new_description>"])
    prompt = fake_claude.calls()[0]["argv"][1]
    assert "When the user mentions forms." in prompt
    assert "1536" in prompt and "1024" in prompt
    assert "will be truncated" not in prompt
    assert "make me a pdf form" in prompt and "flaky infra query" not in prompt


def test_rewrite_retries_an_invalid_description_then_succeeds(fake_claude, tmp_area):
    res = improve(fake_claude, ["<new_description>Use <b>bold</b> PDFs.</new_description>",
                                "<new_description>Use for PDF forms.</new_description>"])
    assert res.description == "Use for PDF forms." and res.attempts == 2 and len(res.calls) == 2
    assert "'<' or '>'" in fake_claude.calls()[1]["argv"][1]


def test_rewrite_rejects_overlong_and_fails_clearly_after_three(fake_claude, tmp_area):
    too_long = "<new_description>" + "a" * 1025 + "</new_description>"
    with pytest.raises(imp.RewriteFailed) as exc:
        improve(fake_claude, [too_long])
    assert "1025" in str(exc.value) and len(exc.value.calls) == 3


def test_rewrite_enforces_combined_listing_cap(fake_claude, tmp_area):
    md = SKILL_MD.replace("When the user mentions forms.", "w" * 900)
    with pytest.raises(imp.RewriteFailed) as exc:
        improve(fake_claude, ["<new_description>" + "a" * 700 + "</new_description>"], skill_md=md)
    assert "1536" in str(exc.value)


def test_rewrite_missing_tags_is_retried(fake_claude, tmp_area):
    res = improve(fake_claude, ["no tags here", "<new_description>Fine.</new_description>"])
    assert res.description == "Fine."


def test_rewrite_call_error_fails_without_retry(fake_claude, tmp_area):
    fake_claude.respond({"exit_code": 1, "is_error": True, "stderr": "not logged in"})
    with pytest.raises(imp.RewriteFailed) as exc:
        imp.improve_description(NAME, SKILL_MD, "d", TRAIN_PQ, [], "m")
    assert "not logged in" in str(exc.value) and len(fake_claude.calls()) == 1


def test_improve_cli_prints_description(fake_claude, tmp_area, tmp_path):
    skill = tmp_path / NAME
    skill.mkdir()
    (skill / "SKILL.md").write_text(SKILL_MD)
    ev = tmp_path / "ev.json"
    ev.write_text(json.dumps({"description": "Old description about PDFs.", "results": TRAIN_PQ}))
    fake_claude.respond({"text": "<new_description>Use for PDF forms.</new_description>"})
    res = CliRunner().invoke(imp.main, ["--eval-results", str(ev), "--skill-path", str(skill), "--model", "m",
                                        "--json", "--", "--some-future-flag", "x"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["description"] == "Use for PDF forms."
    assert fake_claude.calls()[0]["argv"][-2:] == ["--some-future-flag", "x"]


# ------------------------------------------------------------ generate_report

def test_report_is_offline_escaped_and_shows_skips_errors_runs(env):
    regress_script(env)
    data = json.loads(run(env).output)
    data["iterations"][0]["train"]["per_query"][0]["query"] = "<script>alert(1)</script>"
    data["error_counts"] = {"error_runs": 2, "error_queries": 1}
    page = gr.generate_html(data)
    assert "<script" not in page and "&lt;script&gt;" in page
    assert "https://" not in page and "http://" not in page
    assert "skipped (train regressed)" in page and "2 error runs" in page
    assert "Runs used" in page and "not applied" in page


def test_report_cli_writes_file(env, tmp_path):
    regress_script(env)
    data = run(env).output
    src = tmp_path / "results.json"
    src.write_text(data)
    out = tmp_path / "r.html"
    res = CliRunner().invoke(gr.main, [str(src), "-o", str(out), "--json"])
    assert res.exit_code == 0 and json.loads(res.output)["output"] == str(out)
    assert out.read_text().startswith("<!DOCTYPE html>")


# ------------------------------------------------------------ review fixes

def test_c33_standalone_rewriter_refuses_manual_only_skill_with_zero_calls(fake_claude, tmp_area, tmp_path):
    skill = tmp_path / NAME
    skill.mkdir()
    (skill / "SKILL.md").write_text(SKILL_MD.replace("paths:", "disable-model-invocation: true\npaths:"))
    ev = tmp_path / "ev.json"
    ev.write_text(json.dumps({"description": "d", "results": TRAIN_PQ}))
    res = CliRunner().invoke(imp.main, ["--eval-results", str(ev), "--skill-path", str(skill), "--model", "m"])
    assert res.exit_code != 0 and "disable-model-invocation" in res.output
    assert fake_claude.calls() == []


def test_failed_rewrite_calls_are_recorded_in_results(env):
    script(env.fake, {})
    rules = json.loads(env.fake.script.read_text())["rules"]
    rules.insert(1, {"match": REWRITE_MATCH, "responses": [{"text": "no tags"}]})
    env.fake.set_rules(rules)
    out = json.loads(run(env).output)
    assert out["exit_reason"].startswith("rewrite failed")
    recorded = [c for it in out["iterations"] for c in it["calls"] if c["kind"] == "rewrite"]
    assert len(recorded) == 3 == out["calls"]["rewrites"]


def test_default_results_dirs_never_collide(tmp_path):
    wanted = tmp_path / "ws" / "trigger-opt" / "20261016T000000Z"
    first, second = rl._claim_default_dir(wanted), rl._claim_default_dir(wanted)
    assert first != second and first.is_dir() and second.is_dir()


def test_unsafe_skill_name_refused_before_any_artifact_or_call(env):
    (env.skill / "SKILL.md").write_text(SKILL_MD.replace(f"name: {NAME}", "name: ../../.claude/commands/foo"))
    before = protected(env)
    res = run(env, expect_ok=False)
    assert res.exit_code != 0
    assert env.fake.calls() == [] and protected(env) == before
    assert not (env.project / ".." / ".claude").resolve().joinpath("commands", "foo").exists()


def test_canary_that_never_launched_is_not_counted_as_a_call(env):
    script(env.fake, {})
    res = run(env, "--max-total-usd", "0", expect_ok=False)
    assert res.exit_code == 3 and env.fake.calls() == []
    data = json.loads((only_results_dir(env) / "results.json").read_text())
    assert data["calls"]["made"] == 0 and data["calls"]["canary"] == 0


def test_results_never_go_inside_another_skill_or_skills_dir(env, tmp_path):
    other = tmp_path / "other-skill"
    other.mkdir()
    (other / "SKILL.md").write_text("---\nname: other-skill\ndescription: d\n---\n")
    res = run(env, "--results-dir", str(other / "out"), expect_ok=False)
    assert res.exit_code != 0 and "inside the skill directory" in res.output
    shared = tmp_path / "home2" / ".claude" / "skills" / "x"
    res = run(env, "--results-dir", str(shared), expect_ok=False)
    assert res.exit_code != 0 and env.fake.calls() == []
    (tmp_path / "plugin" / ".claude-plugin").mkdir(parents=True)
    res = run(env, "--results-dir", str(tmp_path / "plugin" / "skills" / "out"), expect_ok=False)
    assert res.exit_code != 0 and env.fake.calls() == []


def test_add_dir_holding_a_same_named_skill_is_refused(env, tmp_path):
    ext = tmp_path / "ext"
    (ext / ".claude" / "skills" / NAME).mkdir(parents=True)
    res = run(env, "--", "--add-dir", str(ext), expect_ok=False)
    assert res.exit_code != 0 and "already holds a skill named" in res.output
    assert env.fake.calls() == []


def test_relative_add_dir_is_refused_before_any_call(env):
    res = run(env, "--", "--add-dir", "../ext", expect_ok=False)
    assert res.exit_code != 0 and "absolute path" in res.output
    assert env.fake.calls() == []

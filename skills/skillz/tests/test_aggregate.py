"""Tests for scripts/eval/aggregate_benchmark.py (criteria 14-20, 22)."""
import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

import aggregate_benchmark as ab
from conftest import FIXTURES, SKILL_ROOT

WS = Path(FIXTURES) / "workspaces"


def copy_ws(tmp_path, name):
    dest = tmp_path / name
    shutil.copytree(WS / name, dest)
    return dest / "iteration-1"


def aggregate(it, skill="demo-skill"):
    bench = ab.generate_benchmark(it, skill)
    return bench


def jwrite(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))


def grading(passed, total):
    exps = [{"text": f"c{i}", "passed": i < passed, "evidence": "e"} for i in range(total)]
    return {"expectations": exps,
            "summary": {"passed": passed, "failed": total - passed, "total": total,
                        "pass_rate": passed / total},
            "execution_metrics": {"total_tool_calls": 3, "errors_encountered": 0, "output_chars": 777777}}


def make_run(it, cfg, n, *, pass_n=2, total=4, timing=None, run=None, grade="ok", eval_dir="eval-1-x"):
    d = it / eval_dir / cfg / f"run-{n}"
    d.mkdir(parents=True, exist_ok=True)
    if grade == "ok":
        jwrite(d / "grading.json", grading(pass_n, total))
    elif grade != "missing":
        jwrite(d / "grading.json", grade)
    if timing is not None:
        jwrite(d / "timing.json", timing)
    if run is not None:
        jwrite(d / "run.json", run)
    return d


def meta(it, eval_dir="eval-1-x", **kw):
    m = {"eval_id": 1, "eval_name": "x", "baseline_kind": "no_skill", "baseline_label": "no skill"}
    m.update(kw)
    jwrite(it / eval_dir / "eval_metadata.json", m)


def tm(tokens=1000, secs=10.0, prov="cli_result"):
    return {"total_tokens": tokens, "total_duration_seconds": secs, "provenance": prov}


def rj(skills=(), **kw):
    r = {"exit_code": 0, "is_error": False, "permission_denials": [], "permission_denied_events": 0,
         "init": {"skills": list(skills), "model": "m1"}}
    r.update(kw)
    return r


def by(bench, cfg, n=1, eval_id=1):
    return next(r for r in bench["runs"] if r["configuration"] == cfg and r["run_number"] == n
                and r["eval_id"] == eval_id)


def test_c14_canonical_layout_gives_nonempty_benchmark(tmp_path):
    """c14: SKILL.md layout with single run-1/ dirs aggregates."""
    it = copy_ws(tmp_path, "canonical")
    b = aggregate(it)
    assert len(b["runs"]) == 4
    assert all(r["status"] == "succeeded" for r in b["runs"])
    assert b["run_summary"]["with_skill"]["pass_rate"]["mean"] == 1.0
    assert b["run_summary"]["without_skill"]["pass_rate"]["mean"] == 0.25
    assert b["metadata"]["evals_run"] == [1, 2]


def test_c14_cli_writes_files_and_json_flag(tmp_path):
    it = copy_ws(tmp_path, "canonical")
    res = CliRunner().invoke(ab.main, [str(it), "--skill-name", "demo-skill", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout) == json.loads((it / "benchmark.json").read_text())
    md = (it / "benchmark.md").read_text()
    assert "runs_per_configuration" not in md and "Run counts" in md
    assert "--skill-name" in CliRunner().invoke(ab.main, ["--help"]).output


def test_c15_uneven_run_counts(tmp_path):
    """c15: 3 with_skill, 1 without_skill (failed): counts match dirs on disk."""
    it = copy_ws(tmp_path, "uneven")
    b = aggregate(it)
    counts = {c["configuration"]: c for c in b["metadata"]["run_counts"]}
    assert counts["with_skill"] == {"eval_id": 1, "eval_name": "ocean", "configuration": "with_skill",
                                    "discovered": 3, "succeeded": 3, "failed": 0, "contaminated": 0}
    assert counts["without_skill"]["discovered"] == 1 and counts["without_skill"]["failed"] == 1
    assert counts["without_skill"]["succeeded"] == 0
    assert len(b["runs"]) == 4
    assert "runs_per_configuration" not in b["metadata"]
    # a configuration with no succeeded run has null stats, not zeros, and no delta
    assert b["run_summary"]["without_skill"]["pass_rate"] is None
    assert b["run_summary"]["delta"] == {"pass_rate": None, "time_seconds": None, "tokens": None}


@pytest.mark.parametrize("kind,label", [("no_skill", "no skill"), ("snapshot", "v1 (original)")])
def test_c16_delta_sign_by_role(tmp_path, kind, label):
    """c16: delta = with_skill - without_skill for either baseline kind."""
    it = tmp_path / "iteration-1"
    meta(it, baseline_kind=kind, baseline_label=label)
    make_run(it, "with_skill", 1, pass_n=4, timing=tm(3000, 30.0))
    make_run(it, "without_skill", 1, pass_n=1, timing=tm(1000, 10.0))
    b = aggregate(it)
    assert b["run_summary"]["delta"] == {"pass_rate": "+0.75", "time_seconds": "+20.0", "tokens": "+2000"}
    assert list(b["run_summary"]) == ["with_skill", "without_skill", "delta"]
    assert b["metadata"]["baseline_kind"] == kind and b["metadata"]["baseline_label"] == label
    assert label in ab.generate_markdown(b)
    # baseline better than the candidate gives a negative delta
    make_run(it, "with_skill", 1, pass_n=0, timing=tm(500, 5.0))
    d = aggregate(it)["run_summary"]["delta"]
    assert d == {"pass_rate": "-0.25", "time_seconds": "-5.0", "tokens": "-500"}


def test_c16_runs_emitted_in_role_order(tmp_path):
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "without_skill", 1)
    make_run(it, "with_skill", 1)
    assert [r["configuration"] for r in aggregate(it)["runs"]] == ["with_skill", "without_skill"]


def test_c16_baseline_disagreement_errors(tmp_path):
    it = tmp_path / "iteration-1"
    meta(it)
    meta(it, eval_dir="eval-2-y", eval_id=2, baseline_kind="snapshot", baseline_label="v1")
    make_run(it, "with_skill", 1)
    make_run(it, "with_skill", 1, eval_dir="eval-2-y")
    with pytest.raises(ab.AggregateError):
        aggregate(it)


def test_c17_eval_name_propagates_from_eval_dir(tmp_path):
    """c17: eval_metadata.json at the eval level reaches runs[] and run_counts[]."""
    it = copy_ws(tmp_path, "canonical")
    b = aggregate(it)
    assert {r["eval_name"] for r in b["runs"]} == {"preserves-formulas", "handles-empty"}
    assert {c["eval_name"] for c in b["metadata"]["run_counts"]} == {"preserves-formulas", "handles-empty"}
    assert by(b, "with_skill", eval_id=2)["eval_name"] == "handles-empty"


def test_c17_eval_id_falls_back_to_dir_name(tmp_path):
    it = tmp_path / "iteration-1"
    make_run(it, "with_skill", 1, eval_dir="eval-7-slug")
    b = aggregate(it)
    assert b["runs"][0]["eval_id"] == 7 and b["runs"][0]["eval_name"] is None


@pytest.mark.parametrize("grade,reason", [
    ("missing", "grading.json missing"),
    ("{not json", "grading.json is not valid JSON"),
    ({"summary": {}}, "grading.json has no expectations array"),
    ({"expectations": []}, "grading.json has no valid summary"),
    ({"expectations": [], "summary": {}}, "grading.json has no valid summary"),
    ({"expectations": [], "summary": {"passed": 1, "failed": 0, "total": "1", "pass_rate": 1}},
     "grading.json has no valid summary"),
    ([1, 2], "grading.json has no expectations array"),
])
def test_c18_bad_grading_is_failed_not_dropped(tmp_path, grade, reason):
    """c18: missing or malformed grading.json is listed as failed."""
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "with_skill", 1, pass_n=4)
    make_run(it, "with_skill", 2, grade=grade)
    b = aggregate(it)
    bad = by(b, "with_skill", 2)
    assert bad["status"] == "failed" and bad["status_reason"] == reason
    assert bad["result"] is None and bad["expectations"] == []
    assert b["metadata"]["run_counts"][0]["discovered"] == 2
    assert b["metadata"]["run_counts"][0]["failed"] == 1
    # excluded from the mean: only run 1 (pass rate 1.0) counts
    assert b["run_summary"]["with_skill"]["pass_rate"]["mean"] == 1.0
    assert "failed" in ab.generate_markdown(b) and reason in ab.generate_markdown(b)


@pytest.mark.parametrize("run,reason_part", [
    (rj(exit_code=2), "exit_code 2"),
    (rj(is_error=True), "is_error true"),
    (rj(permission_denials=[{"tool_name": "Write"}]), "permission denied: Write"),
    (rj(permission_denied_events=2), "permission denied"),
    (rj(status="error", error="no result event", is_error=None), "no result event"),
    (rj(status="error", error=None, exit_code=None, is_error=None), "status error"),
])
def test_c18_run_errors_fail_the_run(tmp_path, run, reason_part):
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "with_skill", 1, run=run)
    r = aggregate(it)["runs"][0]
    assert r["status"] == "failed" and reason_part in r["status_reason"]
    assert r["result"] is not None  # grading was readable, but the run is excluded
    assert aggregate(it)["run_summary"]["with_skill"]["pass_rate"] is None


def test_c18_missing_run_json_is_not_a_failure(tmp_path):
    """Subagent fallback: no run.json, grading fine -> succeeded."""
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "with_skill", 1)
    make_run(it, "without_skill", 1)
    b = aggregate(it)
    assert [r["status"] for r in b["runs"]] == ["succeeded", "succeeded"]
    assert b["metadata"]["executor_model"] is None


def test_c19_missing_timing_gives_null_tokens_with_provenance(tmp_path):
    """c19: no timing.json -> tokens null / 'missing', never output_chars (777777) or 0."""
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "with_skill", 1)
    r = aggregate(it)["runs"][0]["result"]
    assert r["tokens"] is None and r["tokens_provenance"] == "missing"
    assert r["time_seconds"] is None
    b = aggregate(it)
    assert b["run_summary"]["with_skill"]["tokens"] is None
    assert b["metadata"]["token_provenance"] == ["missing"]


def test_c19_wall_clock_timing_has_null_tokens(tmp_path):
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "with_skill", 1, timing={"total_tokens": None, "total_duration_seconds": 5.0,
                                          "provenance": "wall_clock"})
    r = aggregate(it)["runs"][0]["result"]
    assert r["tokens"] is None and r["tokens_provenance"] == "wall_clock" and r["time_seconds"] == 5.0


@pytest.mark.parametrize("prov", ["cli_result", "subagent_notification"])
def test_c19_real_provenance_without_token_count_is_missing(tmp_path, prov):
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "with_skill", 1, timing={"total_tokens": None, "total_duration_seconds": 5.0,
                                          "provenance": prov})
    b = aggregate(it)
    assert b["runs"][0]["result"]["tokens"] is None
    assert b["runs"][0]["result"]["tokens_provenance"] == "missing"
    assert b["metadata"]["token_provenance"] == ["missing"]


def test_c19_time_falls_back_to_grading_timing(tmp_path):
    it = tmp_path / "iteration-1"
    meta(it)
    g = grading(2, 4)
    g["timing"] = {"total_duration_seconds": 7.5}
    d = make_run(it, "with_skill", 1)
    jwrite(d / "grading.json", g)
    assert aggregate(it)["runs"][0]["result"]["time_seconds"] == 7.5


def test_c19_mixed_token_provenance_is_flagged(tmp_path):
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "with_skill", 1, timing=tm(100, prov="cli_result"))
    make_run(it, "with_skill", 2, timing=tm(100, prov="subagent_notification"))
    b = aggregate(it)
    assert b["metadata"]["token_provenance"] == ["cli_result", "subagent_notification"]
    assert any("mix provenances" in n for n in b["notes"])
    # no combined token aggregate; per-run values stay
    assert b["run_summary"]["with_skill"]["tokens"] is None
    assert b["run_summary"]["delta"]["tokens"] is None
    assert [r["result"]["tokens"] for r in b["runs"]] == [100, 100]
    make_run(it, "without_skill", 1, timing=tm(50, prov="cli_result"))
    assert aggregate(it)["run_summary"]["delta"]["tokens"] is None


@pytest.mark.parametrize("skills,contaminated", [
    (["demo-skill"], True),
    (["myplugin:demo-skill"], True),
    (["other", "demo-skill"], True),
    (["demo-skill-2"], False),
    (["not-demo-skill"], False),
    (["demo-skill:other"], False),
    ([], False),
])
def test_c20_contamination_matching(tmp_path, skills, contaminated):
    """c20: exact or <prefix>:<name> match only, in without_skill only."""
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "with_skill", 1, pass_n=4, timing=tm(100, 1.0), run=rj(["demo-skill"]))
    make_run(it, "without_skill", 1, pass_n=0, timing=tm(50, 1.0), run=rj(skills))
    b = aggregate(it)
    base = by(b, "without_skill")
    assert (base["status"] == "contaminated") is contaminated
    assert by(b, "with_skill")["status"] == "succeeded"  # never applies to with_skill
    if contaminated:
        assert base["status_reason"] == 'init.skills contains candidate "demo-skill"'
        assert base["result"] is not None
        assert b["run_summary"]["without_skill"]["pass_rate"] is None
        assert b["run_summary"]["delta"]["pass_rate"] is None
        counts = next(c for c in b["metadata"]["run_counts"] if c["configuration"] == "without_skill")
        assert counts["contaminated"] == 1 and counts["succeeded"] == 0
        assert any("contaminated" in n for n in b["notes"])
    else:
        assert b["run_summary"]["delta"]["pass_rate"] == "+1.00"


@pytest.mark.parametrize("recorded", [False, True])
def test_c20_recorded_verdict_wins_for_same_name_snapshot(tmp_path, recorded):
    """c20: a same-name snapshot baseline lists the candidate's name in
    init.skills by design; run_tasks' recorded verdict decides."""
    it = tmp_path / "iteration-1"
    meta(it, baseline_kind="snapshot", baseline_label="v1 (original)")
    make_run(it, "with_skill", 1, pass_n=4, timing=tm(100, 1.0), run=rj(["demo-skill"]))
    make_run(it, "without_skill", 1, pass_n=0, timing=tm(50, 1.0),
             run=rj(["demo-skill"], contaminated=recorded))
    base = by(aggregate(it), "without_skill")
    assert base["status"] == ("contaminated" if recorded else "succeeded")


def test_c20_failed_takes_precedence_over_contaminated(tmp_path):
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "without_skill", 1, grade="missing", run=rj(["demo-skill"]))
    r = aggregate(it)["runs"][0]
    assert r["status"] == "failed"


def test_executor_model_most_common_and_noted(tmp_path):
    it = tmp_path / "iteration-1"
    meta(it)
    for n, model in ((1, "a"), (2, "a"), (3, "b")):
        r = rj()
        r["init"]["model"] = model
        make_run(it, "with_skill", n, run=r)
    b = aggregate(it)
    assert b["metadata"]["executor_model"] == "a" and b["metadata"]["analyzer_model"] is None
    assert any("executor_model differs" in n for n in b["notes"])


def test_no_eval_dirs_is_an_error(tmp_path):
    (tmp_path / "iteration-1").mkdir()
    res = CliRunner().invoke(ab.main, [str(tmp_path / "iteration-1"), "--skill-name", "x"])
    assert res.exit_code == 1


def test_c22_no_assertions_key_anywhere(tmp_path):
    """c22: output and source use `expectations`, never `assertions`."""
    it = copy_ws(tmp_path, "canonical")
    aggregate(it)
    CliRunner().invoke(ab.main, [str(it), "--skill-name", "demo-skill"])
    text = (it / "benchmark.json").read_text() + (it / "benchmark.md").read_text()
    assert "assertions" not in text.lower()
    assert "expectations" in text
    src = Path(ab.__file__).read_text().lower()
    assert "assertions" not in src


@pytest.mark.parametrize("content,reason", [
    ("{not json", "run.json is not valid JSON"),
    ("[1]", "run.json is not a JSON object"),
    ('"x"', "run.json is not a JSON object"),
])
def test_review_broken_run_json_fails_run(tmp_path, content, reason):
    """A malformed or non-object run.json is failed, even for a baseline that could hide contamination."""
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "without_skill", 1)
    jwrite(it / "eval-1-x/without_skill/run-1/run.json", content)
    r = aggregate(it)["runs"][0]
    assert r["status"] == "failed" and r["status_reason"] == reason


@pytest.mark.parametrize("run,field", [
    ({}, "exit_code"),
    (rj(exit_code=False), "exit_code"),
    (rj(is_error="no"), "is_error"),
    ({k: v for k, v in rj().items() if k != "permission_denials"}, "permission_denials"),
    (rj(permission_denied_events=None), "permission_denied_events"),
])
def test_review_schema_invalid_run_json_fails_run(tmp_path, run, field):
    """A run.json object missing a required field, or holding the wrong type, is failed."""
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "with_skill", 1, run=run)
    r = aggregate(it)["runs"][0]
    assert r["status"] == "failed" and r["status_reason"] == f"run.json: {field} missing or invalid"


def test_review_wall_clock_tokens_are_dropped(tmp_path):
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "with_skill", 1, timing={"total_tokens": 900, "total_duration_seconds": 5.0,
                                          "provenance": "wall_clock"})
    r = aggregate(it)["runs"][0]["result"]
    assert r["tokens"] is None and r["tokens_provenance"] == "wall_clock"


@pytest.mark.parametrize("prov", ["estimate", None, 3])
def test_review_unknown_provenance_is_missing(tmp_path, prov):
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "with_skill", 1, timing={"total_tokens": 900, "total_duration_seconds": 5.0,
                                          "provenance": prov})
    r = aggregate(it)["runs"][0]["result"]
    assert r["tokens"] is None and r["tokens_provenance"] == "missing"


def test_review_two_place_rounded_pass_rate_is_valid(tmp_path):
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "with_skill", 1, grade={"expectations": [], "summary": {
        "passed": 2, "failed": 1, "total": 3, "pass_rate": 0.67}})
    assert aggregate(it)["runs"][0]["status"] == "succeeded"


@pytest.mark.parametrize("summary", [
    {"passed": -1, "failed": 5, "total": 4, "pass_rate": 0.5},
    {"passed": 1, "failed": 1, "total": 3, "pass_rate": 0.5},
    {"passed": 2, "failed": 2, "total": 4, "pass_rate": 1.5},
    {"passed": 2, "failed": 2, "total": 4, "pass_rate": -0.5},
    {"passed": 1, "failed": 3, "total": 4, "pass_rate": 0.9},
    {"passed": 1, "failed": 2, "total": 3, "pass_rate": 0.34},
    {"passed": 0, "failed": 0, "total": 0, "pass_rate": 1.0},
    {"passed": 2, "failed": 2, "total": 4, "pass_rate": True},
])
def test_review_inconsistent_summary_fails_run(tmp_path, summary):
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "with_skill", 1, grade={"expectations": [], "summary": summary})
    r = aggregate(it)["runs"][0]
    assert r["status"] == "failed" and r["status_reason"] == "grading.json has no valid summary"


def test_review_zero_total_summary_is_valid(tmp_path):
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "with_skill", 1, grade={"expectations": [], "summary": {
        "passed": 0, "failed": 0, "total": 0, "pass_rate": 0.0}})
    assert aggregate(it)["runs"][0]["status"] == "succeeded"


@pytest.mark.parametrize("bad", [12.7, True, "5", -3, [1]])
def test_review_non_integer_tokens_are_null_never_truncated(tmp_path, bad):
    it = tmp_path / "iteration-1"
    meta(it)
    make_run(it, "with_skill", 1, timing={"total_tokens": bad, "total_duration_seconds": 1.0,
                                          "provenance": "cli_result"})
    r = aggregate(it)["runs"][0]["result"]
    assert r["tokens"] is None and r["tokens_provenance"] == "missing"

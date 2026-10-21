"""Tests for scripts/eval/generate_review.py and assets/viewer.html (unit U7)."""

import json
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import generate_review as gr
from conftest import SKILL_ROOT

SCRIPT = SKILL_ROOT / "scripts" / "eval" / "generate_review.py"
VIEWER = SKILL_ROOT / "assets" / "viewer.html"


def make_run(ws, eval_dir="eval-1-ocean", config="with_skill", n=1, text="hello", meta=True, meta_extra=None):
    """Create <ws>/<eval_dir>/<config>/run-<n>/outputs/answer.txt (+ eval_metadata.json at eval level)."""
    ev = ws / eval_dir
    run = ev / config / f"run-{n}"
    (run / "outputs").mkdir(parents=True)
    (run / "outputs" / "answer.txt").write_text(text)
    md = ev / "eval_metadata.json"
    if meta and not md.exists():
        data = {"eval_id": 1, "eval_name": "ocean", "prompt": "Describe the ocean",
                "baseline_kind": "snapshot", "baseline_label": "v1 (original)"}
        data.update(meta_extra or {})
        md.write_text(json.dumps(data))
    return run


def embedded(html):
    m = re.search(r"const EMBEDDED_DATA = (.*?);\n", html, re.S)
    return json.loads(m.group(1))


def run_cli(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, timeout=60)


# ---- criterion 17: eval_metadata.json found at the eval-directory level ----

def test_c17_eval_metadata_found_two_levels_up(tmp_path):
    ws = tmp_path / "iteration-1"
    make_run(ws)
    (run,) = gr.find_runs(ws)
    assert run["prompt"] == "Describe the ocean"
    assert run["eval_id"] == 1
    assert run["eval_name"] == "ocean"
    assert run["baseline_label"] == "v1 (original)"
    assert run["configuration"] == "with_skill"
    assert run["run_number"] == 1


def test_c17_eval_metadata_not_taken_from_sibling_eval(tmp_path):
    ws = tmp_path / "iteration-1"
    make_run(ws)
    make_run(ws, eval_dir="eval-2-lake", meta=False)
    by_dir = {r["id"]: r for r in gr.find_runs(ws)}
    assert by_dir["eval-2-lake-with_skill-run-1"]["prompt"] == "(No prompt found)"
    assert by_dir["eval-1-ocean-with_skill-run-1"]["eval_name"] == "ocean"


def test_c17_viewer_section_header_uses_eval_name():
    html = VIEWER.read_text()
    assert "evalRuns[0].eval_name" in html and '"Eval " + evalId' in html


# ---- role order, labels, grading ----

def test_role_order_with_skill_first_and_numeric_run_order(tmp_path):
    ws = tmp_path / "iteration-1"
    make_run(ws, config="without_skill", n=1)
    make_run(ws, config="with_skill", n=10)
    make_run(ws, config="with_skill", n=2)
    got = [(r["configuration"], r["run_number"]) for r in gr.find_runs(ws)]
    assert got == [("with_skill", 2), ("with_skill", 10), ("without_skill", 1)]


def test_sort_key_orders_by_role_not_id():
    a = {"id": "a", "eval_id": 1, "configuration": "without_skill", "run_number": 1}
    b = {"id": "z", "eval_id": 1, "configuration": "with_skill", "run_number": 1}
    assert sorted([a, b], key=gr._run_sort_key)[0] is b


def test_baseline_label_embedded_without_benchmark(tmp_path):
    ws = tmp_path / "iteration-1"
    make_run(ws)
    data = embedded(gr.generate_html(gr.find_runs(ws), "s"))
    assert data["baseline_label"] == "v1 (original)"


def test_grading_read_from_run_dir_only(tmp_path):
    ws = tmp_path / "iteration-1"
    run = make_run(ws)
    (run / "grading.json").write_text(json.dumps({"summary": {"pass_rate": 1}, "expectations": []}))
    make_run(ws, config="without_skill")
    runs = {r["id"]: r for r in gr.find_runs(ws)}
    assert runs["eval-1-ocean-with_skill-run-1"]["grading"]["summary"]["pass_rate"] == 1
    assert runs["eval-1-ocean-without_skill-run-1"]["grading"] is None


# ---- viewer.html static checks ----

def test_viewer_uses_new_contract_names():
    html = VIEWER.read_text()
    assert "runs_per_configuration" not in html
    for token in ("run_counts", "baseline_label", "status_reason", "typeof XLSX"):
        assert token in html
    assert "assertion" not in html.lower()
    # badge regex must keep working for fixed directory names
    assert "(with_skill|without_skill|new_skill|old_skill)" in html


def test_external_scripts_never_block_parsing():
    """A black-holed CDN request must not stall the inline viewer script."""
    html = VIEWER.read_text()
    tags = re.findall(r"<script\b[^>]*\bsrc=[^>]*>", html)
    assert tags and all(re.search(r"\basync\b|\bdefer\b", t) for t in tags)
    assert not re.search(r'<link[^>]*rel="stylesheet"(?![^>]*media="print")', html)  # fonts load non-blocking


def test_template_path_resolves_from_file():
    assert gr.VIEWER_TEMPLATE == VIEWER and VIEWER.is_file()


# ---- criterion 24: --static renders text outputs with no network ----

def test_c24_static_embeds_text_output_inline(tmp_path):
    ws = tmp_path / "iteration-1"
    make_run(ws, text="UNIQUE-OUTPUT-TEXT-42")
    out = tmp_path / "out" / "review.html"
    r = run_cli(ws, "--static", out, "--json", "--skill-name", "demo")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["mode"] == "static"
    html = out.read_text()
    data = embedded(html)
    (f,) = data["runs"][0]["outputs"]
    assert f == {"name": "answer.txt", "type": "text", "content": "UNIQUE-OUTPUT-TEXT-42"}
    # The only external resources are optional extras: fonts and the SheetJS script.
    urls = set(re.findall(r'(?:src|href)="(https?://[^/"]+)', html))
    assert urls <= {"https://fonts.googleapis.com", "https://cdn.sheetjs.com"}
    # Text is rendered by the viewer itself (textContent), not fetched.
    assert 'file.type === "text"' in html and "pre.textContent = file.content" in html


def test_c24_static_survives_script_end_tag_in_output(tmp_path):
    ws = tmp_path / "iteration-1"
    make_run(ws, text="a </script><b>x</b> <!-- c")
    out = tmp_path / "review.html"
    assert run_cli(ws, "--static", out).returncode == 0
    html = out.read_text()
    assert html.count("</script>") == VIEWER.read_text().count("</script>")
    assert embedded(html)["runs"][0]["outputs"][0]["content"] == "a </script><b>x</b> <!-- c"


def test_static_includes_benchmark_and_previous_feedback(tmp_path):
    ws = tmp_path / "iteration-2"
    make_run(ws)
    prev = tmp_path / "iteration-1"
    make_run(prev, text="old")
    (prev / "feedback.json").write_text(json.dumps(
        {"reviews": [{"run_id": "eval-1-ocean-with_skill-run-1", "feedback": "needs labels", "timestamp": "t"}],
         "status": "complete"}))
    bench = tmp_path / "benchmark.json"
    bench.write_text(json.dumps({"metadata": {"baseline_label": "v1 (original)"}, "runs": [], "run_summary": {}}))
    out = tmp_path / "review.html"
    r = run_cli(ws, "--static", out, "--previous-workspace", prev, "--benchmark", bench)
    assert r.returncode == 0, r.stderr
    data = embedded(out.read_text())
    assert data["previous_feedback"] == {"eval-1-ocean-with_skill-run-1": "needs labels"}
    assert data["previous_outputs"]["eval-1-ocean-with_skill-run-1"][0]["content"] == "old"
    assert data["benchmark"]["metadata"]["baseline_label"] == "v1 (original)"


# ---- CLI surface ----

def test_help_and_empty_workspace(tmp_path):
    h = run_cli("--help")
    assert h.returncode == 0 and "--static" in h.stdout and "--json" in h.stdout
    (tmp_path / "empty").mkdir()
    r = run_cli(tmp_path / "empty")
    assert r.returncode != 0 and "No runs found" in r.stderr


# ---- criterion 23: never kill; busy port falls back ----

def test_c23_source_has_no_process_killing():
    src = SCRIPT.read_text()
    for banned in ("SIGTERM", "lsof", "os.kill", "_kill_port", "subprocess", "signal"):
        assert banned not in src, banned


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(ws, port, env):
    p = subprocess.Popen([sys.executable, str(SCRIPT), str(ws), "--port", str(port), "--json"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    return p, json.loads(p.stdout.readline())


def _get(url, path="/"):
    with urllib.request.urlopen(url + path, timeout=10) as r:
        return r.status, r.read().decode()


@pytest.fixture
def servers(monkeypatch):
    monkeypatch.setenv("BROWSER", "true")  # webbrowser.open must not launch a real browser
    import os
    procs = []
    yield procs, dict(os.environ)
    for p in procs:
        p.terminate()
        p.wait(timeout=10)


def test_c23_two_viewers_same_port_both_serve(tmp_path, servers):
    procs, env = servers
    ws = tmp_path / "iteration-1"
    make_run(ws, text="served-text")
    port = _free_port()
    p1, info1 = _start(ws, port, env)
    procs.append(p1)
    assert info1["port"] == port
    p2, info2 = _start(ws, port, env)
    procs.append(p2)
    assert info2["port"] != port
    assert p1.poll() is None, "first viewer was killed"
    for info in (info1, info2):
        status, body = _get(info["url"])
        assert status == 200 and "served-text" in body


def test_feedback_roundtrip_and_validation(tmp_path, servers):
    procs, env = servers
    ws = tmp_path / "iteration-1"
    make_run(ws)
    p, info = _start(ws, _free_port(), env)
    procs.append(p)
    assert json.loads(_get(info["url"], "/api/feedback")[1]) == {}
    payload = {"reviews": [{"run_id": "eval-1-ocean-with_skill-run-1", "feedback": "ok", "timestamp": "t"}],
               "status": "complete"}
    req = urllib.request.Request(info["url"] + "/api/feedback", data=json.dumps(payload).encode(), method="POST")
    assert urllib.request.urlopen(req, timeout=10).status == 200
    assert json.loads((ws / "feedback.json").read_text()) == payload
    assert json.loads(_get(info["url"], "/api/feedback")[1]) == payload
    bad = urllib.request.Request(info["url"] + "/api/feedback", data=b'{"nope":1}', method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(bad, timeout=10)
    assert e.value.code == 500
    assert json.loads((ws / "feedback.json").read_text()) == payload


# ---- viewer behaviour, executed under node with a minimal DOM stub ----

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const data = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const script = html.split('<script>').pop().split('</script>')[0]
  .replace('/*__EMBEDDED_DATA__*/', 'var EMBEDDED_DATA = ' + JSON.stringify(data) + ';');
const esc = t => String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const out = {};
function el(id) {
  const e = { style: {}, dataset: {}, children: [], value: '', className: '',
    classList: { add(){}, remove(){}, toggle(){}, contains(){return false} },
    appendChild(c){ this.children.push(c); return c; }, addEventListener(){}, setAttribute(){},
    set textContent(v){ this._t = v; }, get textContent(){ return this._t; },
    set innerHTML(v){ this._h = v; if (id) out[id] = v; },
    get innerHTML(){ return this._h !== undefined ? this._h : esc(this._t); } };
  return e;
}
const doc = { getElementById: id => el(id), createElement: () => el(null), querySelector: () => el(null),
  querySelectorAll: () => [], addEventListener(){} , body: el(null) };
const ctx = { document: doc, window: {}, fetch: () => Promise.reject(new Error('offline')), console,
  setTimeout, clearTimeout, atob: s => Buffer.from(s,'base64').toString('binary'), Uint8Array, Blob: function(){},
  URL: { createObjectURL(){}, revokeObjectURL(){} } };
ctx.window = ctx;
if (process.argv[4] === 'xlsx-missing') { /* XLSX intentionally undefined */ }
vm.createContext(ctx);
vm.runInContext(script, ctx);
if (process.argv[4] === 'xlsx-missing') {
  const c = el(null);
  vm.runInContext('renderXlsx', ctx)(c, 'AAAA', 'book.xlsx');
  out.xlsx_children = c.children.map(x => ({ tag: x.className, href: x.href, text: x.textContent }));
}
process.stdout.write(JSON.stringify(out));
"""

pytestmark_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run_viewer(tmp_path, data, *extra):
    h = tmp_path / "harness.js"
    h.write_text(HARNESS)
    d = tmp_path / "data.json"
    d.write_text(json.dumps(data))
    r = subprocess.run(["node", str(h), str(VIEWER), str(d), *extra], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _bench_data(**over):
    run = {"id": "eval-1-ocean-with_skill-run-1", "prompt": "p", "eval_id": 1, "eval_name": "ocean",
           "configuration": "with_skill", "run_number": 1, "outputs": [], "grading": None}
    def r(config, n, status, pr, reason=None, tokens=None):
        return {"eval_id": 1, "eval_name": "ocean", "configuration": config, "run_number": n, "status": status,
                "status_reason": reason,
                "result": None if pr is None else {"pass_rate": pr, "passed": 1, "failed": 0, "total": 1,
                                                    "time_seconds": 2.0, "tokens": tokens, "errors": None},
                "expectations": [], "notes": []}
    bench = {
        "metadata": {"skill_name": "s", "evals_run": [1], "baseline_label": "v1 (original)",
                     "run_counts": [
                         {"eval_id": 1, "eval_name": "ocean", "configuration": "with_skill",
                          "discovered": 3, "succeeded": 2, "failed": 1, "contaminated": 0},
                         {"eval_id": 1, "eval_name": "ocean", "configuration": "without_skill",
                          "discovered": 1, "succeeded": 0, "failed": 0, "contaminated": 1}]},
        "runs": [r("with_skill", 1, "succeeded", 1.0), r("with_skill", 2, "succeeded", 0.5),
                 r("with_skill", 3, "failed", None, "grading.json missing"),
                 r("without_skill", 1, "contaminated", 0.0, 'init.skills contains candidate "s"')],
        "run_summary": {"with_skill": {"pass_rate": {"mean": 0.75, "stddev": 0.25, "min": 0.5, "max": 1.0},
                                       "time_seconds": {"mean": 2.0, "stddev": 0.0, "min": 2.0, "max": 2.0},
                                       "tokens": None},
                        "without_skill": None,
                        "delta": {"pass_rate": None, "time_seconds": None, "tokens": None}},
        "notes": [],
    }
    bench.update(over)
    return {"skill_name": "s", "baseline_label": None, "runs": [run], "previous_feedback": {},
            "previous_outputs": {}, "benchmark": bench}


@pytestmark_node
def test_viewer_run_counts_baseline_label_and_status(tmp_path):
    html = _run_viewer(tmp_path, _bench_data())["benchmark-content"]
    assert "runs_per_configuration" not in html and "runs per configuration" not in html
    assert "With Skill 2/3, v1 (original) 0/1 succeeded" in html
    assert "Without Skill" not in html
    # per eval x configuration count rows, failed and contaminated visible
    assert re.search(r"<td>ocean</td><td>With Skill</td><td>3</td><td>2</td><td>1</td><td>0</td>", html)
    assert re.search(r"<td>ocean</td><td>v1 \(original\)</td><td>1</td><td>0</td><td>0</td><td>1</td>", html)
    assert "failed &mdash; grading.json missing" in html
    assert "benchmark-row-failed" in html and "benchmark-row-contaminated" in html
    assert "<h4" in html and ">ocean</h4>" in html


@pytestmark_node
def test_viewer_averages_use_succeeded_runs_only_and_tolerate_nulls(tmp_path):
    html = _run_viewer(tmp_path, _bench_data())["benchmark-content"]
    # with_skill: succeeded 1.0 and 0.5 average to 75%; the failed run (null result) is excluded
    assert "<td>Avg</td><td>2/3 succeeded</td><td class=\"benchmark-delta-positive\">75%</td>" in html or \
        re.search(r"<td>Avg</td><td>2/3 succeeded</td><td class=\"[^\"]*\">75%</td>", html)
    # contaminated baseline has 0% pass rate but is excluded: average is n/a
    assert re.search(r"<td>Avg</td><td>0/1 succeeded</td><td class=\"\">n/a</td>", html)
    # null tokens, null run_summary entry, null deltas -> n/a, no crash
    assert "<td><strong>Tokens</strong></td><td>n/a</td><td>n/a</td><td class=\"\">n/a</td>" in html
    assert "NaN" not in html and "undefined" not in html and "null" not in html


@pytestmark_node
def test_viewer_null_run_summary_does_not_crash(tmp_path):
    out = _run_viewer(tmp_path, _bench_data(run_summary=None))
    assert "Pass Rate" in out["benchmark-content"] and "n/a" in out["benchmark-content"]


@pytestmark_node
def test_viewer_xlsx_falls_back_to_download_link_without_sheetjs(tmp_path):
    out = _run_viewer(tmp_path, _bench_data(), "xlsx-missing")
    (child,) = out["xlsx_children"]
    assert child["href"].startswith("data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,AAAA")
    assert "book.xlsx" in child["text"]

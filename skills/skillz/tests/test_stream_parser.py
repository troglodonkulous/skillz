"""Stream parser, trigger detection and outcome classification (offline)."""

import json

import pytest

import claude_cli as cc
from conftest import FIXTURES

STREAMS = FIXTURES / "stream-json"
CANARY = "zq-pelican-canary"


def load(name):
    return cc.parse_stream((STREAMS / name).read_text())


def outcome(name, kind=cc.TRIGGER, returncode=0, skill=CANARY):
    s = load(name)
    trig = cc.detect_trigger(s, skill) if kind == cc.TRIGGER else None
    return cc.classify(kind, returncode, False, s, trig)


# ---- criterion 35: the six recorded fixtures ----

@pytest.mark.parametrize("name", [
    "triggered_partial_isolated.jsonl",
    "triggered_partial_default-sources.jsonl",
])
def test_c35_triggered_fixtures(name):
    assert outcome(name) == ("triggered", None)


@pytest.mark.parametrize("name", [
    "not-triggered_tool-call-before-end_partial_isolated.jsonl",
    "not-triggered_plain_say-ok.jsonl",
])
def test_c35_not_triggered_fixtures(name):
    assert outcome(name) == ("not_triggered", None)


def test_c35_permission_denied_is_error_with_exit_0():
    status, reason = outcome("permission-denied_dontAsk.jsonl")
    assert status == "error" and "Write" in reason


def test_c35_not_logged_in_is_error_despite_subtype_success():
    s = load("error_not-logged-in.jsonl")
    assert s.subtype == "success" and s.is_error is True
    # exit 0 and exit 1 both classify as error: is_error alone is enough
    assert outcome("error_not-logged-in.jsonl", returncode=0)[0] == "error"
    assert outcome("error_not-logged-in.jsonl", returncode=1)[0] == "error"


# ---- criterion 29: other tools before/around the Skill call ----

def test_c29_tool_call_before_end_fixture_has_tool_but_no_skill():
    s = load("not-triggered_tool-call-before-end_partial_isolated.jsonl")
    assert [t["name"] for t in s.tool_uses if t["source"] == "assistant"] == ["Bash"]
    assert cc.detect_trigger(s, CANARY) is False


def test_c29_triggered_fixtures_detected_by_both_paths():
    for name in ("triggered_partial_isolated.jsonl", "triggered_partial_default-sources.jsonl"):
        s = load(name)
        assert {t["source"] for t in s.tool_uses if t["name"] == "Skill"} == {"assistant", "partial"}


def ev(obj):
    return json.dumps(obj)


def assistant(name, inp):
    return ev({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t", "name": name, "input": inp}]}})


def test_c29_glob_before_skill_is_a_trigger():
    stream = "\n".join([assistant("Glob", {"pattern": "*"}), assistant("Skill", {"skill": "foo"})])
    assert cc.detect_trigger(cc.parse_stream(stream), "foo") is True


def partial_skill(fragments, name="Skill", index=0):
    out = [ev({"type": "stream_event", "event": {"type": "message_start"}}),
           ev({"type": "stream_event", "event": {"type": "content_block_start", "index": index,
               "content_block": {"type": "tool_use", "id": "t", "name": name, "input": {}}}})]
    for f in fragments:
        out.append(ev({"type": "stream_event", "event": {"type": "content_block_delta", "index": index,
                       "delta": {"type": "input_json_delta", "partial_json": f}}}))
    out.append(ev({"type": "stream_event", "event": {"type": "content_block_stop", "index": index}}))
    return out


def test_partial_only_stream_triggers():
    lines = partial_skill(["", '{"sk', 'ill": "fo', 'o"}'])
    assert cc.detect_trigger(cc.parse_stream(lines), "foo") is True


def test_partial_without_stop_still_parsed_at_end_of_stream():
    lines = partial_skill(['{"skill": "foo"}'])[:-1]
    assert cc.detect_trigger(cc.parse_stream(lines), "foo") is True


def test_incomplete_partial_json_is_not_a_match():
    lines = partial_skill(['{"skill": "fo'])
    assert cc.detect_trigger(cc.parse_stream(lines), "foo") is False


# ---- no substring matching ----

@pytest.mark.parametrize("skill_in_stream", ["foo-bar", "my-foo", "foobar", "FOO", "foo "])
def test_skill_name_must_match_exactly(skill_in_stream):
    s = cc.parse_stream(assistant("Skill", {"skill": skill_in_stream}))
    assert cc.detect_trigger(s, "foo") is False


def test_prefixed_skill_name_matches():
    s = cc.parse_stream(assistant("Skill", {"skill": "plugin:foo"}))
    assert cc.detect_trigger(s, "foo") is True
    assert cc.detect_trigger(s, "plugin") is False


def test_other_tool_named_input_skill_does_not_match():
    s = cc.parse_stream(assistant("Bash", {"skill": "foo", "command": "echo foo"}))
    assert cc.detect_trigger(s, "foo") is False


def test_read_of_skill_md_matches_with_skill_path(tmp_path):
    sdir = tmp_path / "p" / ".claude" / "skills" / "foo"
    sdir.mkdir(parents=True)
    s = cc.parse_stream(assistant("Read", {"file_path": str(sdir / "SKILL.md")}))
    assert cc.detect_trigger(s, "foo", sdir) is True


def test_read_of_other_file_in_skill_dir_does_not_match(tmp_path):
    sdir = tmp_path / "foo"
    sdir.mkdir()
    s = cc.parse_stream(assistant("Read", {"file_path": str(sdir / "references" / "x.md")}))
    assert cc.detect_trigger(s, "foo", sdir) is False


def test_read_without_skill_path_never_matches():
    """An identically named skill elsewhere (e.g. via --add-dir) cannot score."""
    s = cc.parse_stream(assistant("Read", {"file_path": "/elsewhere/.claude/skills/foo/SKILL.md"}))
    assert cc.detect_trigger(s, "foo") is False


# ---- parse details ----

def test_hook_events_before_init_are_tolerated():
    s = load("triggered_partial_default-sources.jsonl")
    assert s.init is not None and s.claude_code_version == "2.1.300"


def test_init_and_result_fields():
    s = load("triggered_partial_isolated.jsonl")
    assert CANARY in s.skills
    assert s.model == "claude-haiku-5-5"
    assert isinstance(s.tools, list) and isinstance(s.slash_commands, list)
    assert s.total_cost_usd == pytest.approx(0.0051116)
    assert s.is_error is False and s.num_turns is not None and s.duration_ms is not None
    assert s.terminal_reason == s.result["terminal_reason"]


def test_total_tokens_is_sum_of_four_usage_fields():
    u = {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 4, "cache_read_input_tokens": 8,
         "ignored": 1000}
    s = cc.parse_stream(ev({"type": "result", "usage": u, "is_error": False}))
    assert s.total_tokens == 15
    assert cc.parse_stream("").total_tokens is None


def test_denials_from_event_only_and_from_result_only_and_deduped():
    event = ev({"type": "system", "subtype": "permission_denied", "tool_name": "Write", "tool_use_id": "a"})
    res = ev({"type": "result", "is_error": False,
              "permission_denials": [{"tool_name": "Write", "tool_use_id": "a"},
                                     {"tool_name": "Bash", "tool_use_id": "b"}]})
    assert len(cc.parse_stream([event]).denials) == 1
    assert len(cc.parse_stream([res]).denials) == 2
    assert len(cc.parse_stream([event, res]).denials) == 2
    # the permission-denied fixture carries both and counts once
    assert len(load("permission-denied_dontAsk.jsonl").denials) == 1


def test_garbage_lines_are_counted_not_fatal():
    s = cc.parse_stream(["not json", "", "[1]", ev({"type": "result", "is_error": False})])
    assert s.bad_lines == 2 and s.result is not None


# ---- classification ----

def good_summary(**result):
    r = {"type": "result", "is_error": False, "subtype": "success"}
    r.update(result)
    return cc.parse_stream(ev(r))


def test_classification_order_and_reasons():
    ok = good_summary()
    assert cc.classify(cc.TRIGGER, 0, False, ok, True) == ("triggered", None)
    assert cc.classify(cc.TRIGGER, 0, False, ok, False) == ("not_triggered", None)
    assert cc.classify(cc.EXECUTION, 0, False, ok) == ("ok", None)
    assert cc.classify(cc.REWRITE, 0, False, ok) == ("ok", None)
    assert cc.classify(cc.TRIGGER, 1, False, ok, True)[0] == "error"
    assert cc.classify(cc.TRIGGER, 0, True, ok, True) == ("error", "timeout")
    assert cc.classify(cc.TRIGGER, 0, False, cc.parse_stream(""), False) == ("error", "no result event")
    assert cc.classify(cc.TRIGGER, 0, False, good_summary(is_error=True), True)[0] == "error"


def test_a_match_does_not_rescue_an_errored_run():
    s = cc.parse_stream([assistant("Skill", {"skill": "foo"}), ev({"type": "result", "is_error": True})])
    assert cc.detect_trigger(s, "foo")
    assert cc.classify(cc.TRIGGER, 0, False, s, True)[0] == "error"


def test_subtype_is_never_used():
    err_sub = good_summary(subtype="error_during_execution")
    assert cc.classify(cc.TRIGGER, 0, False, err_sub, True) == ("triggered", None)
    bad = good_summary(subtype="success", is_error=True)
    assert cc.classify(cc.TRIGGER, 0, False, bad, False)[0] == "error"

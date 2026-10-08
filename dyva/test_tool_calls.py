"""Tests for _drop_nameless_tool_calls — pytest (`uv run pytest dyva/test_tool_calls.py`).

A host can stream a garbage/empty tool call ({"function":{"name":null,...}}); the client
echoes it back and an OpenAI-typed host 400s on name=None, killing the whole race. These
pin that nameless calls are dropped before any dialect conversion.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dyva   # noqa: E402


def test_drops_the_only_nameless_call_and_removes_key():
    msgs = [{"role": "assistant",
             "tool_calls": [{"function": {"name": None, "arguments": {}}, "index": 0}]}]
    dyva._drop_nameless_tool_calls(msgs)
    assert "tool_calls" not in msgs[0]          # nothing real left -> key removed entirely


def test_keeps_named_drops_nameless_in_mix():
    msgs = [{"role": "assistant", "tool_calls": [
        {"function": {"name": "do_x", "arguments": "{}"}, "id": "a"},
        {"function": {"name": None, "arguments": {}}},
    ]}]
    dyva._drop_nameless_tool_calls(msgs)
    assert [tc["function"]["name"] for tc in msgs[0]["tool_calls"]] == ["do_x"]


def test_blank_name_is_dropped():
    msgs = [{"role": "assistant", "tool_calls": [{"function": {"name": "   "}}]}]
    dyva._drop_nameless_tool_calls(msgs)
    assert "tool_calls" not in msgs[0]


def test_leaves_normal_messages_untouched():
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"}]
    before = [dict(m) for m in msgs]
    dyva._drop_nameless_tool_calls(msgs)
    assert msgs == before


def test_tolerates_none_and_nondict_entries():
    dyva._drop_nameless_tool_calls(None)              # no crash
    dyva._drop_nameless_tool_calls(["x", 5, {"role": "user"}])


def test_merge_concatenates_argument_fragments_by_index():
    import json
    frags = [
        {"index": 0, "id": "call_1", "function": {"name": "web", "arguments": '{"query": "Cal'}},
        {"index": 0, "function": {"arguments": "tech IPAC"}},
        {"index": 0, "function": {"arguments": ' github"}'}},
    ]
    out = dyva._merge_tool_call_fragments(frags)
    assert len(out) == 1
    assert out[0]["id"] == "call_1" and out[0]["function"]["name"] == "web"
    assert out[0]["function"]["arguments"] == '{"query": "Caltech IPAC github"}'
    assert json.loads(out[0]["function"]["arguments"]) == {"query": "Caltech IPAC github"}


def test_merge_keeps_distinct_indexes_separate():
    frags = [
        {"index": 0, "function": {"name": "a", "arguments": "{}"}},
        {"index": 1, "function": {"name": "b", "arguments": '{"x":1}'}},
    ]
    out = dyva._merge_tool_call_fragments(frags)
    assert [c["function"]["name"] for c in out] == ["a", "b"]


def test_merge_no_index_each_is_its_own_call():
    frags = [{"function": {"name": "a", "arguments": "{}"}},
             {"function": {"name": "b", "arguments": "{}"}}]
    assert len(dyva._merge_tool_call_fragments(frags)) == 2


def test_merge_empty():
    assert dyva._merge_tool_call_fragments([]) == []
    assert dyva._merge_tool_call_fragments(None) == []


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))

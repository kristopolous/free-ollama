"""Tests for prefill vs aside continuation shaping — pytest
(`uv run pytest dyva/test_prefill_continuation.py`).

When a partial answer is carried to the NEXT host (skip / stall / drop / context
reissue), dyva shapes it per host: a PREFILL (open-ended final assistant turn +
continue_final_message) on servers that honour the flags (vLLM / llama.cpp), and the
universal ASIDE fallback (ends on a user turn) everywhere else. These pin the pure
shaping + sizing logic — no host is contacted.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dyva   # noqa: E402


def _services(monkeypatch, mapping):
    """Pin service_of() to a fixed host->service map."""
    monkeypatch.setattr(dyva, "_service_index", dict(mapping))


def test_supports_prefill_only_for_known_flag_honouring_servers(monkeypatch):
    _services(monkeypatch, {"vl": "vllm", "lc": "llama.cpp", "ol": "ollama",
                            "lm": "lmstudio", "sg": "sglang"})
    assert dyva._supports_prefill("vl") is True
    assert dyva._supports_prefill("lc") is True
    # OpenAI-dialect but NOT known to honour the flags -> must NOT prefill (silent restart).
    assert dyva._supports_prefill("lm") is False
    assert dyva._supports_prefill("sg") is False
    # Native ollama -> no.
    assert dyva._supports_prefill("ol") is False


def test_apply_continuation_prefill_shape(monkeypatch):
    _services(monkeypatch, {"vl": "vllm"})
    p = {"messages": [{"role": "user", "content": "hi"}]}
    out = dyva._apply_continuation(p, "The answer is", "vl", attempt_oai=True)
    # flags set, ends on an OPEN-ENDED assistant turn holding the RAW partial (no aside)
    assert out["continue_final_message"] is True
    assert out["add_generation_prompt"] is False
    assert out["messages"][-1] == {"role": "assistant", "content": "The answer is"}
    # original payload not mutated
    assert "continue_final_message" not in p
    assert len(p["messages"]) == 1


def test_apply_continuation_aside_shape_for_non_prefill_host(monkeypatch):
    _services(monkeypatch, {"lm": "lmstudio"})
    p = {"messages": [{"role": "user", "content": "hi"}]}
    out = dyva._apply_continuation(p, "The answer is", "lm", attempt_oai=True)
    assert "continue_final_message" not in out
    # ends on a USER turn (valid on every dialect)
    assert out["messages"][-1]["role"] == "user"
    # the assistant aside carries the partial + the self-interruption
    assert out["messages"][-2]["role"] == "assistant"
    assert out["messages"][-2]["content"].startswith("The answer is")
    assert "think about this some more" in out["messages"][-2]["content"]


def test_apply_continuation_native_dialect_never_prefills_even_on_vllm(monkeypatch):
    # If the OpenAI endpoint missed and we fall back to the native dialect, the flags
    # don't apply there — must be the aside shape.
    _services(monkeypatch, {"vl": "vllm"})
    p = {"messages": [{"role": "user", "content": "hi"}]}
    out = dyva._apply_continuation(p, "partial", "vl", attempt_oai=False)
    assert "continue_final_message" not in out
    assert out["messages"][-1]["role"] == "user"


def test_deprefill_reverts_to_aside(monkeypatch):
    # A prefill-shaped request (flags + open-ended assistant) reverts cleanly.
    p = {"messages": [{"role": "user", "content": "hi"},
                      {"role": "assistant", "content": "partial"}],
         "continue_final_message": True, "add_generation_prompt": False,
         "model": "m"}
    out = dyva._deprefill(p, "partial", attempt_oai=True)
    assert "continue_final_message" not in out
    assert "add_generation_prompt" not in out
    assert out["model"] == "m"
    # the open-ended assistant turn is dropped and replaced by the aside (ends on user)
    assert out["messages"][-1]["role"] == "user"
    assert out["messages"][0] == {"role": "user", "content": "hi"}


def test_estimate_prompt_tokens_counts_carried_partial():
    base = {"messages": [{"role": "user", "content": "hello world"}]}
    with_cont = dict(base, _dyva_continue="a b c d e f g h i j")
    assert dyva._estimate_prompt_tokens(with_cont) > dyva._estimate_prompt_tokens(base)


def test_continuation_payload_accumulates_marker_and_bumps_ctx():
    p = {"messages": [{"role": "user", "content": "hi"}], "options": {"num_ctx": 2048}}
    out1 = dyva._continuation_payload(p, "first")
    assert out1["_dyva_continue"] == "first"
    # a second hop accumulates rather than replacing
    out2 = dyva._continuation_payload(out1, "second")
    assert out2["_dyva_continue"] == "firstsecond"
    # num_ctx only grows (never shrinks)
    assert out2["options"]["num_ctx"] >= 2048
    # the partial is NOT injected into messages here — _send_chat shapes it per host
    assert out2["messages"] == [{"role": "user", "content": "hi"}]

"""Tests for the host_status ;key-op-value filter compiler.
`uv run pytest dyva/test_host_status_where.py`
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dyva   # noqa: E402

COLS = {"host", "model", "state", "tps", "ttft", "fail_smoke", "deprio"}


@pytest.fixture(autouse=True)
def _seed_columns(monkeypatch):
    # _split_host_facets consults the real schema via _host_status_columns(); seed the cache
    # so the split tests are deterministic and never touch the DB.
    monkeypatch.setattr(dyva, "_hs_columns_cache", set(COLS))


def test_empty_query():
    assert dyva.host_status_where("", COLS) == ("", [])
    assert dyva.host_status_where("   ;  ; ", COLS) == ("", [])


def test_single_equality_binds_text():
    assert dyva.host_status_where(";state=good", COLS) == ("state = ?", ["good"])


def test_numeric_value_coerced():
    assert dyva.host_status_where(";tps>10", COLS) == ("tps > ?", [10])
    assert dyva.host_status_where(";ttft<0.5", COLS) == ("ttft < ?", [0.5])


def test_two_char_ops_win_over_one_char():
    assert dyva.host_status_where(";tps>=10", COLS) == ("tps >= ?", [10])
    assert dyva.host_status_where(";ttft<=500", COLS) == ("ttft <= ?", [500])


def test_multiple_clauses_anded_in_order():
    sql, params = dyva.host_status_where(";tps>10;state=good;fail_smoke=0", COLS)
    assert sql == "tps > ? AND state = ? AND fail_smoke = ?"
    assert params == [10, "good", 0]


def test_key_is_lowercased_and_trimmed():
    assert dyva.host_status_where(" ; TPS > 10 ", COLS) == ("tps > ?", [10])


def test_unknown_key_rejected():
    with pytest.raises(ValueError, match="unknown filter key"):
        dyva.host_status_where(";rm -rf=1", COLS)
    with pytest.raises(ValueError, match="unknown filter key"):
        dyva.host_status_where(";password=x", COLS)


def test_clause_without_operator_rejected():
    with pytest.raises(ValueError, match="no operator"):
        dyva.host_status_where(";justaword", COLS)


def test_injection_value_is_bound_not_interpolated():
    # a malicious value can't escape — it becomes a bound param; the SQL fragment
    # only ever names a whitelisted column + a placeholder
    sql, params = dyva.host_status_where(";state=good' OR '1'='1", COLS)
    assert sql == "state = ?"
    assert params == ["good' OR '1'='1"]


def test_semicolon_laden_injection_is_split_and_rejected():
    # a payload with its own ';' splits into clauses; the junk clause has no
    # operator and is rejected outright (also safe, just via a 400 not a bind)
    with pytest.raises(ValueError, match="no operator"):
        dyva.host_status_where(";state=good'; DROP TABLE host_status;--", COLS)


def test_split_no_semicolon_is_all_model_token():
    # the common path: no ';' -> whole thing is the model token, no facets
    assert dyva._split_host_facets("qwen3") == ("qwen3", None)


def test_split_model_size_date_predicate_stays_in_model_token():
    # the model token keeps its own size/date predicates; only what's after the
    # first ';' becomes facets
    assert dyva._split_host_facets("qwen>2026-02>5gb") == ("qwen>2026-02>5gb", None)
    assert dyva._split_host_facets("qwen>2026-02>5gb;tps>20") == ("qwen>2026-02>5gb", "tps>20")


def test_split_peels_one_facet():
    assert dyva._split_host_facets("qwen3;tps>20") == ("qwen3", "tps>20")


def test_split_leading_semicolon_empty_model_token():
    assert dyva._split_host_facets(";tps>20;state=good") == ("", "tps>20;state=good")


def test_split_multiple_facets():
    assert dyva._split_host_facets("qwen3;tps>10;state=good") == ("qwen3", "tps>10;state=good")


def test_split_is_order_independent_facets_first():
    # facets before the model name parse the same as after it (';tps>40;qwen' == 'qwen;tps>40')
    assert dyva._split_host_facets(";tps>40;qwen") == ("qwen", "tps>40")
    assert dyva._split_host_facets("tps>40;qwen") == ("qwen", "tps>40")
    assert dyva._split_host_facets("qwen;tps>40") == ("qwen", "tps>40")


def test_split_unknown_key_stays_in_model_token():
    # a facet-shaped segment whose key isn't a column is kept as model text (narrows to
    # nothing visibly) rather than silently dropped
    assert dyva._split_host_facets("qwen;country=JP") == ("qwen country=JP", None)


def test_facet_host_set_ignores_unknown_key(monkeypatch):
    # an unknown facet key is rejected by host_status_where; _facet_host_set logs and
    # returns None (no reduction) rather than letting a typo break routing. No DB hit.
    monkeypatch.setattr(dyva, "_hs_columns_cache", {"tps", "state", "host", "model"})
    assert dyva._facet_host_set("bogus>1") is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

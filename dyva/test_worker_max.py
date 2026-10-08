"""Tests for the absolute worker run-time cap decision.
`uv run pytest dyva/test_worker_max.py`
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dyva   # noqa: E402


def test_deadline_is_start_plus_max():
    assert dyva._stream_deadline(1000.0, 1800) == 2800.0


def test_zero_max_disables():
    assert dyva._stream_deadline(1000.0, 0) is None


def test_negative_max_disables():
    assert dyva._stream_deadline(1000.0, -5) is None


def test_unknown_start_disables():
    assert dyva._stream_deadline(None, 1800) is None
    assert dyva._stream_deadline(0, 1800) is None


def test_large_cap_allowed():
    # 20 days — the point is it's not silently clamped in the deadline math
    twenty_days = 20 * 86400
    assert dyva._stream_deadline(100.0, twenty_days) == 100.0 + twenty_days


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))

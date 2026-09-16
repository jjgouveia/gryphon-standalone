"""Tests for compact estimated context savings metadata."""

from __future__ import annotations

import json

from gryphon.context_savings import (
    estimate_context_savings,
    estimate_file_tokens,
    estimate_tokens,
    format_context_savings,
)


def test_estimate_tokens_uses_conservative_character_approximation():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_estimate_context_savings_returns_tiny_metadata():
    estimate = estimate_context_savings(
        original_tokens=100,
        returned_context="x" * 80,
    )

    assert estimate == {
        "estimated": True,
        "saved_tokens": 80,
        "saved_percent": 80,
    }
    assert len(json.dumps(estimate, separators=(",", ":"))) < 64


def test_estimate_context_savings_never_reports_negative_savings():
    estimate = estimate_context_savings(
        original_tokens=10,
        returned_context="x" * 200,
    )

    assert estimate == {
        "estimated": True,
        "saved_tokens": 0,
        "saved_percent": 0,
    }


def test_estimate_context_savings_unknown_original_returns_none():
    assert estimate_context_savings(original_tokens=0, returned_context="x") is None


def test_estimate_file_tokens_uses_file_sizes_without_reading_contents(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("x" * 17, encoding="utf-8")

    assert estimate_file_tokens(tmp_path, ["source.py", "missing.py"]) == 5


def test_format_context_savings_is_one_short_line():
    text = format_context_savings(
        {"estimated": True, "saved_tokens": 1240, "saved_percent": 18}
    )

    assert text == "Estimated context saved: ~1,240 tokens (~18%)"


def test_tool_call_log_entry_carries_session_task_as_ref(tmp_path):
    from gryphon import hints
    from gryphon.context_savings import attach_context_savings
    from gryphon.savings_log import read_entries

    hints.reset_session()
    try:
        hints.get_session().task = "review PR #208"
        result = attach_context_savings(
            {},
            original_tokens=1000,
            returned_context="{}",
            tool="detect_changes_tool",
            repo_root=tmp_path,
        )
    finally:
        hints.reset_session()

    assert result["context_savings"]["saved_tokens"] > 0
    entry = read_entries(tmp_path)[-1]
    assert entry["kind"] == "tool_call"
    assert entry["tool"] == "detect_changes_tool"
    assert entry["ref"] == "review PR #208"


def test_tool_call_log_entry_omits_ref_without_session_task(tmp_path):
    from gryphon import hints
    from gryphon.context_savings import attach_context_savings
    from gryphon.savings_log import read_entries

    hints.reset_session()
    attach_context_savings(
        {},
        original_tokens=1000,
        returned_context="{}",
        tool="detect_changes_tool",
        repo_root=tmp_path,
    )

    assert "ref" not in read_entries(tmp_path)[-1]

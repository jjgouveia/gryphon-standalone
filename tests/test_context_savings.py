"""Tests for compact estimated context savings metadata."""

from __future__ import annotations

import json
from pathlib import Path

from gryphon.context_savings import (
    estimate_context_savings,
    estimate_file_tokens,
    estimate_tokens,
    format_context_savings,
)
from gryphon.graph import GraphStore
from gryphon.parser import EdgeInfo, NodeInfo
from gryphon.savings_log import read_entries


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


# ---------------------------------------------------------------------------
# Savings coverage for the PR-review tool flow
# ---------------------------------------------------------------------------


def _seed_review_repo(tmp_path: Path) -> Path:
    """Small on-disk repo + graph: handle() in app.py calls check() in auth.py."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gryphon").mkdir()
    (repo / "app.py").write_text(
        "def handle():\n    return check()\n" + "# pad\n" * 300,
        encoding="utf-8",
    )
    (repo / "auth.py").write_text(
        "def check():\n    return True\n" + "# pad\n" * 300,
        encoding="utf-8",
    )

    app_path = (repo / "app.py").as_posix()
    auth_path = (repo / "auth.py").as_posix()
    store = GraphStore(str(repo / ".gryphon" / "graph.db"))
    try:
        store.upsert_node(NodeInfo(
            kind="File", name="app.py", file_path=app_path,
            line_start=1, line_end=302, language="python",
        ))
        store.upsert_node(NodeInfo(
            kind="File", name="auth.py", file_path=auth_path,
            line_start=1, line_end=302, language="python",
        ))
        store.upsert_node(NodeInfo(
            kind="Function", name="handle", file_path=app_path,
            line_start=1, line_end=2, language="python",
        ))
        store.upsert_node(NodeInfo(
            kind="Function", name="check", file_path=auth_path,
            line_start=1, line_end=2, language="python",
        ))
        store.upsert_edge(EdgeInfo(
            kind="CALLS",
            source=f"{app_path}::handle",
            target=f"{auth_path}::check",
            file_path=app_path, line=2,
        ))
        store.commit()

        from gryphon.flows import store_flows, trace_flows
        store_flows(store, trace_flows(store))
    finally:
        store.close()
    return repo


def _tool_call_entries(repo: Path, tool: str) -> list[dict]:
    return [
        e for e in read_entries(repo)
        if e.get("kind") == "tool_call" and e.get("tool") == tool
    ]


class TestReviewFlowSavingsCoverage:
    """Every tool used by the review-pr flow must emit a savings record."""

    def test_get_minimal_context_logs_tool_call(self, tmp_path):
        from gryphon.tools.context import get_minimal_context

        repo = _seed_review_repo(tmp_path)
        result = get_minimal_context(
            task="review PR #9",
            changed_files=["app.py"],
            repo_root=str(repo),
        )

        assert result["status"] == "ok"
        assert result["context_savings"]["estimated"] is True
        entries = _tool_call_entries(repo, "get_minimal_context_tool")
        assert len(entries) == 1
        assert entries[0]["ref"] == "review PR #9"
        assert entries[0]["extra"]["counterfactual"]["total_counterfactual"] > 0

    def test_query_graph_logs_tool_call(self, tmp_path):
        from gryphon.tools.query import query_graph

        repo = _seed_review_repo(tmp_path)
        result = query_graph(
            pattern="callers_of", target="check", repo_root=str(repo),
        )

        assert result["status"] == "ok"
        assert result["result_count"] == 1
        assert result["context_savings"]["estimated"] is True
        entries = _tool_call_entries(repo, "query_graph_tool")
        assert len(entries) == 1
        assert "counterfactual" in entries[0]["extra"]

    def test_query_graph_minimal_logs_tool_call(self, tmp_path):
        from gryphon.tools.query import query_graph

        repo = _seed_review_repo(tmp_path)
        result = query_graph(
            pattern="callers_of", target="check", repo_root=str(repo),
            detail_level="minimal",
        )

        assert result["context_savings"]["estimated"] is True
        assert len(_tool_call_entries(repo, "query_graph_tool")) == 1

    def test_semantic_search_nodes_logs_tool_call(self, tmp_path):
        from gryphon.tools.query import semantic_search_nodes

        repo = _seed_review_repo(tmp_path)
        result = semantic_search_nodes(query="check", repo_root=str(repo))

        assert result["status"] == "ok"
        assert result["context_savings"]["estimated"] is True
        entries = _tool_call_entries(repo, "semantic_search_nodes_tool")
        assert len(entries) == 1
        assert "counterfactual" in entries[0]["extra"]

    def test_get_flow_logs_tool_call(self, tmp_path):
        from gryphon.tools.flows_tools import get_flow, list_flows

        repo = _seed_review_repo(tmp_path)
        flows = list_flows(repo_root=str(repo))
        assert flows["status"] == "ok" and flows["flows"]

        result = get_flow(flow_id=flows["flows"][0]["id"], repo_root=str(repo))

        assert result["status"] == "ok"
        assert result["context_savings"]["estimated"] is True
        entries = _tool_call_entries(repo, "get_flow_tool")
        assert len(entries) == 1
        assert "counterfactual" in entries[0]["extra"]

    def test_no_results_still_safe_without_baseline(self, tmp_path):
        from gryphon.tools.query import query_graph

        repo = _seed_review_repo(tmp_path)
        result = query_graph(
            pattern="callers_of", target="missing_fn", repo_root=str(repo),
        )

        assert result["status"] == "not_found"
        # No files behind the answer -> no baseline -> no entry, no crash.
        assert "context_savings" not in result


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


def test_repo_savings_log_follows_the_data_dir(tmp_path, monkeypatch):
    """With the graph outside the repo, the per-repo log goes there too."""
    from gryphon.savings_log import log_savings, read_entries

    repo = tmp_path / "repo"
    repo.mkdir()
    data_dir = tmp_path / "external-data"
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    log_savings(repo, kind="tool_call", baseline_tokens=10, returned_tokens=2,
                saved_tokens=8, saved_percent=80, tool="t")
    assert not (repo / ".gryphon").exists()
    assert (data_dir / "savings.jsonl").is_file()
    assert [e["tool"] for e in read_entries(repo)] == ["t"]

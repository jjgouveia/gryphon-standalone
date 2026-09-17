"""Tool: get_minimal_context — ultra-compact context for token-efficient workflows."""

from __future__ import annotations

import logging
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from ..context_savings import attach_file_savings
from ..graph import GraphStore
from ..hints import get_session
from ..incremental import (
    get_changed_files,
    get_db_path,
    incremental_update,
    resolve_incremental_base,
    resolve_review_base,
)
from ..parser import normalize_file_path
from ._common import _get_store, _resolve_root, compact_response, graph_provenance

logger = logging.getLogger(__name__)


def _not_ready(reason: str, summary: str) -> dict[str, Any]:
    """Return a compact response that directs callers to initialize the graph."""
    return {
        "status": "not_ready",
        "reason": reason,
        "summary": summary,
        "next_tool_suggestions": ["build_or_update_graph"],
    }


def _has_git_changes(root: Path, base: str) -> bool:
    """Quick check for uncommitted or diffed changes."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", base, "--"],
            capture_output=True, stdin=subprocess.DEVNULL, text=True,
            cwd=str(root), timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
        # Also check staged/unstaged
        result2 = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, stdin=subprocess.DEVNULL, text=True,
            cwd=str(root), timeout=10,
        )
        return bool(result2.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _auto_refresh_graph(root: Path, store: GraphStore, base: str) -> str | None:
    """Top up a graph whose build commit is still usable as a diff base.

    Runs the same incremental reconciliation ``gryphon update`` performs:
    diff the recorded build commit against the worktree, re-parse what
    changed, drop files that no longer exist, then refresh signatures and
    FTS at the ``"minimal"`` postprocess level so search keeps working.

    Best-effort: returns a short note on success, ``None`` when the
    reconciliation could not run, in which case the caller serves the
    older graph with a staleness note instead of refusing.
    """
    try:
        from .build import _run_postprocess

        result = incremental_update(root, store, base=base)
        _run_postprocess(
            store,
            result,
            "minimal",
            changed_files=result.get("changed_files"),
        )
    except Exception:
        logger.warning("Graph auto-refresh failed", exc_info=True)
        return None
    return (
        "Graph auto-refreshed to HEAD: "
        f"{result.get('files_updated', 0)} file(s) re-indexed."
    )


def get_minimal_context(
    task: str = "",
    changed_files: list[str] | None = None,
    repo_root: str | None = None,
    base: str = "HEAD~1",
) -> dict[str, Any]:
    """Return minimum context an agent needs to start any task (~100 tokens).

    Combines graph stats, top communities, top flows, risk score,
    and suggested next tools into an ultra-compact response. When the graph
    was built at a different Git commit that is still usable as a diff base,
    it is incrementally refreshed first instead of being rejected; only a
    build commit that no longer exists in the clone is reported stale.

    Args:
        task: Natural language description of what the agent is doing
              (e.g. "review PR #42", "debug login timeout").
        changed_files: Explicit changed files. When given, they define the
                       change set: the local diff only refines line ranges
                       within them and never adds a file of its own, so a
                       remote PR can be reviewed from a checkout sitting on
                       an unrelated branch. Auto-detected from git if None.
        repo_root: Repository root path. Auto-detected if None.
        base: Git ref for diff comparison.

    Returns:
        Compact graph context, or ``status: not_ready`` when the graph is
        missing, empty, or built at a commit that cannot be reconciled with
        the checkout.
    """
    if task:
        get_session().task = task
    root = _resolve_root(repo_root)
    db_path = get_db_path(root, read_only=True)
    if not db_path.is_file():
        return _not_ready(
            "missing_graph",
            "No graph database found. Build the graph before requesting context.",
        )

    store, root = _get_store(str(root))
    try:
        base = resolve_review_base(root, base)
        # 1. Quick stats
        stats = store.get_stats()
        if stats.total_nodes == 0:
            return _not_ready(
                "empty_graph",
                "The graph database contains no nodes. Build the graph before requesting context.",
            )

        provenance = graph_provenance(str(root))
        graph_note: str | None = None
        if provenance and provenance.get("head_matches_build") is False:
            # A commit mismatch does not by itself make the graph stale: the
            # build commit may be an ancestor of HEAD (or otherwise diffable
            # against the worktree), in which case an incremental update
            # reconciles it. Only a build commit missing from the clone is
            # genuinely stale.
            incremental_base = resolve_incremental_base(root, store)
            if incremental_base is None:
                return _not_ready(
                    "stale_graph",
                    "The graph was built at a commit that is not available in "
                    "this clone (history rewrite or shallow fetch). Rebuild "
                    "it before requesting context.",
                )
            graph_note = _auto_refresh_graph(root, store, incremental_base)
            if graph_note is None:
                pending = get_changed_files(root, incremental_base)
                shown = ", ".join(pending[:5])
                graph_note = (
                    "Graph is out of date and auto-refresh failed; "
                    f"{len(pending)} file(s) changed since the build are "
                    f"not indexed{': ' + shown if shown else ''}."
                )
            else:
                stats = store.get_stats()

        # 2. Risk from changed files
        risk = "unknown"
        risk_score = 0.0
        top_affected: list[str] = []
        test_gap_count = 0
        analyzed_files: list[str] = []
        analyzed_abs: list[str] = []
        affected_flow_names: list[str] = []
        n_changed_functions = 0
        if changed_files or _has_git_changes(root, base):
            try:
                from ..changes import analyze_changes
                from ..incremental import get_changed_files as _get_changed

                files = changed_files
                if not files:
                    files = _get_changed(root, base)
                if files:
                    analyzed_files = files
                    abs_files = [normalize_file_path(root / f) for f in files]
                    analyzed_abs = abs_files
                    analysis = analyze_changes(
                        store, abs_files, repo_root=str(root), base=base,
                    )
                    risk_score = analysis.get("risk_score", 0.0)
                    risk = (
                        "high" if risk_score > 0.7
                        else "medium" if risk_score > 0.4
                        else "low"
                    )
                    changed_functions = analysis.get("changed_functions", [])
                    n_changed_functions = len(changed_functions)
                    top_affected = [
                        f.get("name", "")
                        for f in changed_functions[:5]
                    ]
                    test_gap_count = len(analysis.get("test_gaps", []))
                    affected_flow_names = [
                        f.get("name", "")
                        for f in analysis.get("affected_flows", [])[:3]
                    ]
            except (
                ImportError, OSError, ValueError,
                sqlite3.Error, subprocess.SubprocessError,
            ):
                logger.debug("Risk analysis failed in get_minimal_context", exc_info=True)

        # 3. Communities. With a change set these are the communities the
        #    changed files sit in; the repo-wide top 3 would say the same
        #    thing for every change and read as if it described this one
        #    (#1017). Without one, the repo-wide view is the answer.
        communities: list[str] = []
        try:
            if analyzed_abs:
                # SQLite caps host parameters (999 by default), so bound the
                # IN list; the busiest files dominate the grouping anyway.
                sample = analyzed_abs[:300]
                placeholders = ",".join("?" for _ in sample)
                rows = store._conn.execute(
                    "SELECT c.name, COUNT(*) AS hits FROM nodes n "
                    "JOIN communities c ON c.id = n.community_id "
                    f"WHERE n.file_path IN ({placeholders}) "
                    "GROUP BY c.id ORDER BY hits DESC LIMIT 3",
                    sample,
                ).fetchall()
            else:
                rows = store._conn.execute(
                    "SELECT name FROM communities ORDER BY size DESC LIMIT 3"
                ).fetchall()
            communities = [r[0] for r in rows]
        except sqlite3.OperationalError:  # nosec B110 — table may not exist yet
            logger.debug("communities table not yet populated")

        # 4. Flows. Reported as ``flows_affected``, so with a change set they
        #    have to be the flows that change actually touches. The repo-wide
        #    top 3 by criticality is only the answer when nothing changed.
        flows: list[str] = []
        if analyzed_files:
            flows = [name for name in affected_flow_names if name]
        else:
            try:
                rows = store._conn.execute(
                    "SELECT name FROM flows ORDER BY criticality DESC LIMIT 3"
                ).fetchall()
                flows = [r[0] for r in rows]
            except sqlite3.OperationalError:  # nosec B110 — may not exist yet
                logger.debug("flows table not yet populated")

        # 5. Suggest next tools based on task keywords
        task_lower = task.lower()
        if any(w in task_lower for w in ("review", "pr", "merge", "diff")):
            suggestions = ["detect_changes", "get_affected_flows", "get_review_context"]
        elif any(w in task_lower for w in ("debug", "bug", "error", "fix")):
            suggestions = ["semantic_search_nodes", "query_graph", "get_flow"]
        elif any(w in task_lower for w in ("refactor", "rename", "dead", "clean")):
            suggestions = ["refactor", "find_large_functions", "get_architecture_overview"]
        elif any(w in task_lower for w in ("onboard", "understand", "explore", "arch")):
            suggestions = [
                "get_architecture_overview", "list_communities", "list_flows",
            ]
        else:
            suggestions = [
                "detect_changes", "semantic_search_nodes",
                "get_architecture_overview",
            ]

        # Build summary
        summary_parts = [
            f"{stats.total_nodes} nodes, {stats.total_edges} edges"
            f" across {stats.files_count} files.",
        ]
        if graph_note:
            summary_parts.append(graph_note)
        if risk != "unknown":
            summary_parts.append(f"Risk: {risk} ({risk_score:.2f}).")
        if test_gap_count:
            summary_parts.append(f"{test_gap_count} test gaps.")

        result = compact_response(
            summary=" ".join(summary_parts),
            key_entities=top_affected or None,
            risk=risk,
            communities=communities or None,
            flows_affected=flows or None,
            next_tool_suggestions=suggestions,
        )
        attach_file_savings(
            result,
            repo_root=root,
            tool="get_minimal_context_tool",
            files=analyzed_files,
            cf_kwargs={
                "changed_functions": n_changed_functions,
                "test_gaps": test_gap_count,
                "tested_functions": max(
                    0, n_changed_functions - test_gap_count
                ),
            },
        )
        return result
    finally:
        store.close()

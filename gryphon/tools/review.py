"""Tools 4, 12, 16: review context, affected flows, detect changes."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from ..changes import analyze_changes, parse_diff_ranges, parse_git_diff_ranges  # noqa: F401
from ..context_savings import (
    attach_context_savings,
    estimate_counterfactual_cost,
    estimate_file_tokens,
)
from ..flows import get_affected_flows as _get_affected_flows
from ..graph import edge_to_dict, node_to_dict
from ..hints import generate_hints, get_session
from ..incremental import (
    get_changed_files,
    get_staged_and_unstaged,
    resolve_review_base,
)
from ..parser import normalize_file_path
from ..uncertainty import unindexed_changes_note
from ._common import (
    _bounded,
    _get_store,
    _resolve_graph_file_paths,
    _shown_of,
    _validate_positive_int,
    ensure_graph_current,
    graph_provenance,
)

logger = logging.getLogger(__name__)

# Hard ceilings shared by the review tools. All three walk the full impact
# radius of a change set, so on a whole-repo diff every list below is
# proportional to the repository, not to the change. The numbers are set
# from measured cost per row against a 5.6k-node graph:
#   node dict ~60 tok, node+risk_score ~118 tok, test gap ~85 tok,
#   source line ~10 tok, flow with full steps ~980 tok, flow metadata ~18 tok.
_MAX_REVIEW_NODES = 100
_MAX_REVIEW_EDGES = 150
_MAX_REVIEW_FILES = 200
_MAX_REVIEW_SOURCE_LINES = 800
_MAX_LINES_PER_FILE = 500
_MAX_CHANGED_FUNCTIONS = 100
_MAX_DETECT_SOURCE_LINES = 600

# ``get_affected_flows`` in standard mode carries a full ``steps`` list per
# flow (~980 tokens each), so 50 flows is still ~49k tokens — #849 was only
# half-closed by capping the count. The ceiling therefore depends on
# detail_level, the same way query.py caps minimal mode at five results.
_MAX_AFFECTED_FLOWS_STANDARD = 25
_MAX_AFFECTED_FLOWS_MINIMAL = 500
# Flow depth varies hugely between codebases, so a flow *count* alone does
# not bound the response. Steps are filled from the most critical flow
# down until this shared budget runs out; the rest keep their metadata and
# are marked ``steps_omitted``.
_MAX_AFFECTED_FLOW_STEPS = 400
_MAX_DETECT_FLOWS = 200

# ``detect_changes`` embeds affected flows for context, not for flow
# spelunking: every flow carries a full ``steps`` list, which is exactly
# what made get_affected_flows return 247k tokens in #849. Callers who want
# step detail should use get_affected_flows_tool, so the embedded copy keeps
# per-flow metadata only.
_DETECT_FLOW_FIELDS = (
    "id", "name", "criticality", "depth", "node_count", "file_count",
)


def _project(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Keep only *fields* on each row, dropping keys the row does not have."""
    return [{k: r[k] for k in fields if k in r} for r in rows]


def _bound_flow_steps(
    flows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Spend a shared step budget across *flows*, most critical first.

    Returns ``(flows, truncated)``. Flows are already sorted by criticality,
    so the ones a reviewer cares about keep their full step list; the tail
    keeps metadata and is marked ``steps_omitted``. Every flow reports
    ``total_steps`` so the untruncated depth is never lost.
    """
    budget = _MAX_AFFECTED_FLOW_STEPS
    truncated = False
    bounded: list[dict[str, Any]] = []
    for flow in flows:
        out = dict(flow)
        steps = out.get("steps") or []
        out["total_steps"] = len(steps)
        if len(steps) > budget:
            out["steps"] = steps[:budget]
            out["steps_omitted"] = True
            truncated = True
        budget -= min(len(steps), budget)
        bounded.append(out)
    return bounded, truncated


# ---------------------------------------------------------------------------
# Tool 4: get_review_context
# ---------------------------------------------------------------------------


def get_review_context(
    changed_files: list[str] | None = None,
    max_depth: int = 2,
    include_source: bool = True,
    max_lines_per_file: int = 200,
    repo_root: str | None = None,
    base: str = "HEAD~1",
    detail_level: str = "standard",
    max_results: int = 50,
    max_files: int = 25,
) -> dict[str, Any]:
    """Generate a focused review context from changed files.

    Builds a token-optimized subgraph + source snippets for code review.

    Args:
        changed_files: Files to review (auto-detected from git diff if omitted).
        max_depth: Impact radius depth (default: 2).
        include_source: Whether to include source code snippets (default: True).
        max_lines_per_file: Max source lines per file in output (default: 200).
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for change detection (default: HEAD~1).
        detail_level: Output detail level.  "standard" returns full context;
            "minimal" returns summary, risk level, changed/impacted file counts,
            top 5 key entity names, test gap count, and next tool suggestions.
            Default: "standard".
        max_results: Maximum graph nodes per list and edges to return
            (default 50; nodes capped at 200, edges at 300). Each list
            carries its untruncated ``*_total`` count.
        max_files: Maximum files to list and to emit source snippets for
            (default 25, capped at 200). A whole-repo diff otherwise inlined
            every tracked file's source. Snippets additionally share an
            800-line budget and ``max_lines_per_file`` is capped at 500.

    Returns:
        Structured review context with subgraph, source snippets, and
        review guidance, plus a ``truncated`` flag.
    """
    _validate_positive_int(max_results, "max_results")
    _validate_positive_int(max_files, "max_files")
    _validate_positive_int(max_lines_per_file, "max_lines_per_file")

    store, root = _get_store(repo_root)
    try:
        graph_note, not_ready = ensure_graph_current(root, store)
        if not_ready is not None:
            return not_ready

        # Get impact radius first
        if changed_files is None:
            base = resolve_review_base(root, base)
            changed_files = get_changed_files(root, base)
            if not changed_files:
                changed_files = get_staged_and_unstaged(root)

        if not changed_files:
            return {
                "status": "ok",
                "summary": "No changes detected. Nothing to review.",
                "context": {},
            }

        graph_files = _resolve_graph_file_paths(store, root, changed_files)
        impact = store.get_impact_radius(graph_files, max_depth=max_depth)
        # Baseline: tokens an agent would read without the graph —
        # the changed files plus every file in the blast radius.
        original_tokens = estimate_file_tokens(
            root, list(set(changed_files) | set(impact["impacted_files"]))
        )
        counterfactual = estimate_counterfactual_cost(
            changed_functions=len(impact["changed_nodes"]),
            impacted_nodes=len(impact["impacted_nodes"]),
            impacted_files=len(impact["impacted_files"]),
            trace_depth=max_depth,
            file_tokens=original_tokens,
        )

        if detail_level == "minimal":
            impacted_count = len(impact["impacted_nodes"])
            if impacted_count > 20:
                risk = "high"
            elif impacted_count > 5:
                risk = "medium"
            else:
                risk = "low"

            key_entities = [
                n.name for n in impact["changed_nodes"][:5]
            ]

            # Count test gaps among changed functions.
            changed_funcs = [
                n for n in impact["changed_nodes"]
                if n.kind == "Function" and not n.is_test
            ]
            test_edges = [
                e for e in impact["edges"] if e.kind == "TESTED_BY"
            ]
            tested_qualified = {e.source_qualified for e in test_edges}
            test_gap_count = sum(
                1 for f in changed_funcs
                if f.qualified_name not in tested_qualified
            )

            summary_parts = [
                f"Review context for {len(changed_files)} changed file(s):",
                f"  - Risk: {risk}",
                f"  - {len(impact['impacted_nodes'])} impacted nodes"
                f" in {len(impact['impacted_files'])} files",
            ]

            result = {
                "status": "ok",
                "summary": "\n".join(summary_parts),
                "risk": risk,
                "changed_file_count": len(changed_files),
                "impacted_file_count": len(impact["impacted_files"]),
                "key_entities": key_entities,
                "test_gaps": test_gap_count,
                "next_tool_suggestions": [
                    "detect_changes_tool",
                    "get_affected_flows_tool",
                    "get_impact_radius_tool",
                ],
            }
            if graph_note:
                result["summary"] = f"{graph_note}\n{result['summary']}"
            attach_context_savings(
                result,
                original_tokens=original_tokens,
                tool="get_review_context_tool",
                repo_root=root,
                counterfactual=counterfactual,
            )
            return result

        # Build review context. Every list below scales with the change set,
        # so each is bounded and reports its untruncated total.
        shown_files, files_total, files_cut = _bounded(
            changed_files, max_files, _MAX_REVIEW_FILES,
        )
        impacted_files, impacted_total, impacted_cut = _bounded(
            impact["impacted_files"], max_files, _MAX_REVIEW_FILES,
        )
        changed_nodes, changed_nodes_total, cn_cut = _bounded(
            impact["changed_nodes"], max_results, _MAX_REVIEW_NODES,
        )
        impacted_nodes, impacted_nodes_total, in_cut = _bounded(
            impact["impacted_nodes"], max_results, _MAX_REVIEW_NODES,
        )
        edges, edges_total, edges_cut = _bounded(
            impact["edges"], max_results, _MAX_REVIEW_EDGES,
        )
        truncated = (
            files_cut or impacted_cut or cn_cut or in_cut or edges_cut
        )

        context: dict[str, Any] = {
            "changed_files": shown_files,
            "changed_files_total": files_total,
            "impacted_files": impacted_files,
            "impacted_files_total": impacted_total,
            "graph": {
                "changed_nodes": [node_to_dict(n) for n in changed_nodes],
                "changed_nodes_total": changed_nodes_total,
                "impacted_nodes": [node_to_dict(n) for n in impacted_nodes],
                "impacted_nodes_total": impacted_nodes_total,
                "edges": [edge_to_dict(e) for e in edges],
                "edges_total": edges_total,
            },
            "truncated": truncated,
        }

        # Add source snippets for the bounded file list, spending a shared
        # line budget. Snippets were 109k of a 134k-token worst case: without
        # a total budget, ``max_lines_per_file`` alone lets N files each
        # contribute a whole file.
        if include_source:
            snippets = {}
            per_file = min(max_lines_per_file, _MAX_LINES_PER_FILE)
            budget = _MAX_REVIEW_SOURCE_LINES
            for rel_path in shown_files:
                if budget <= 0:
                    context["source_truncated"] = True
                    context["truncated"] = True
                    break
                full_path = root / rel_path
                if full_path.is_file():
                    try:
                        lines = full_path.read_text(encoding="utf-8",
                            errors="replace"
                        ).splitlines()
                        allowed = min(per_file, budget)
                        if len(lines) > allowed:
                            # Include only the relevant functions/classes
                            relevant_lines = _extract_relevant_lines(
                                lines,
                                impact["changed_nodes"],
                                str(full_path),
                                allowed,
                            )
                            snippets[rel_path] = relevant_lines
                            budget -= allowed
                        else:
                            snippets[rel_path] = "\n".join(
                                f"{i+1}: {line}"
                                for i, line in enumerate(lines)
                            )
                            budget -= len(lines)
                    except (OSError, UnicodeDecodeError):
                        snippets[rel_path] = "(could not read file)"
            context["source_snippets"] = snippets

        # Generate review guidance
        guidance = _generate_review_guidance(impact, changed_files)
        context["review_guidance"] = guidance

        summary_parts = [
            f"Review context for {files_total} changed file(s)"
            + _shown_of(len(shown_files), files_total) + ":",
            f"  - {changed_nodes_total} directly changed nodes"
            + _shown_of(len(changed_nodes), changed_nodes_total),
            f"  - {impacted_nodes_total} impacted nodes"
            f" in {impacted_total} files"
            + _shown_of(len(impacted_nodes), impacted_nodes_total),
            "",
            "Review guidance:",
            guidance,
        ]

        result = {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "context": context,
        }
        if graph_note:
            result["summary"] = f"{graph_note}\n{result['summary']}"
        attach_context_savings(
            result,
            original_tokens=original_tokens,
            tool="get_review_context_tool",
            repo_root=root,
            counterfactual=counterfactual,
        )
        return result
    finally:
        store.close()


def _extract_relevant_lines(
    lines: list[str], nodes: list, file_path: str, max_lines: int = 200,
) -> str:
    """Extract only the lines relevant to changed nodes.

    Bounded by *max_lines*: a file where every function changed merges into
    one range covering the whole file, which would defeat the caller's
    ``max_lines_per_file`` budget entirely.
    """
    ranges = []
    for n in nodes:
        if n.file_path == file_path:
            start = max(0, n.line_start - 3)  # 2 lines context before
            end = min(len(lines), n.line_end + 2)  # 1 line context after
            ranges.append((start, end))

    if not ranges:
        # Show first N lines as fallback
        return "\n".join(
            f"{i+1}: {line}" for i, line in enumerate(lines[:min(50, max_lines)])
        )

    # Merge overlapping ranges
    ranges.sort()
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        if start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    parts: list[str] = []
    emitted = 0
    for start, end in merged:
        if emitted >= max_lines:
            parts.append("... (truncated)")
            break
        if parts:
            parts.append("...")
        for i in range(start, min(end, start + max_lines - emitted)):
            parts.append(f"{i+1}: {lines[i]}")
            emitted += 1

    return "\n".join(parts)


def _generate_review_guidance(
    impact: dict, changed_files: list[str]
) -> str:
    """Generate review guidance based on the impact analysis."""
    guidance_parts = []

    # Check for test coverage
    changed_funcs = [
        n for n in impact["changed_nodes"] if n.kind == "Function"
    ]
    test_edges = [e for e in impact["edges"] if e.kind == "TESTED_BY"]
    tested_funcs = {e.source_qualified for e in test_edges}

    untested = [
        f for f in changed_funcs
        if f.qualified_name not in tested_funcs and not f.is_test
    ]
    if untested:
        guidance_parts.append(
            f"- {len(untested)} changed function(s) lack test coverage: "
            + ", ".join(n.name for n in untested[:5])
        )

    # Check for wide blast radius
    if len(impact["impacted_nodes"]) > 20:
        guidance_parts.append(
            f"- Wide blast radius: {len(impact['impacted_nodes'])} "
            "nodes impacted. "
            "Review callers and dependents carefully."
        )

    # Check for inheritance changes
    inheritance_edges = [
        e for e in impact["edges"]
        if e.kind in ("INHERITS", "IMPLEMENTS")
    ]
    if inheritance_edges:
        guidance_parts.append(
            f"- {len(inheritance_edges)} inheritance/implementation "
            "relationship(s) affected. "
            "Check for Liskov substitution violations."
        )

    # Check for cross-file impact
    impacted_file_count = len(impact["impacted_files"])
    if impacted_file_count > 3:
        guidance_parts.append(
            f"- Changes impact {impacted_file_count} other files."
            " Consider splitting into smaller PRs."
        )

    if not guidance_parts:
        guidance_parts.append(
            "- Changes appear well-contained with minimal blast radius."
        )

    return "\n".join(guidance_parts)


# ---------------------------------------------------------------------------
# Tool 12: get_affected_flows  [REVIEW]
# ---------------------------------------------------------------------------


def get_affected_flows_func(
    changed_files: list[str] | None = None,
    base: str = "HEAD~1",
    repo_root: str | None = None,
    detail_level: str = "standard",
    max_flows: int = 50,
) -> dict[str, Any]:
    """Find execution flows affected by changed files.

    [REVIEW] Identifies which execution flows pass through nodes in the
    changed files.  Useful during code review to understand which user-facing
    or critical paths are affected by a change.

    Args:
        changed_files: List of changed file paths (relative to repo root).
                       Auto-detected from git diff if omitted.
        base: Git ref for auto-detecting changes (default: HEAD~1).
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" for full step details, "minimal" for
            per-flow metadata only (name, criticality, depth, counts).
            Every flow carries a full ``steps`` list in standard mode, so
            large change sets can exceed 200k tokens without a bound (#849).
        max_flows: Maximum flows to return (default: 50). ``total`` always
            reports the untruncated count; 0 means "no caller limit".
            Standard mode additionally caps the visible flows at 25 and
            minimal mode at 500, because one standard flow costs ~980
            tokens against ~18 for a minimal one. This mirrors the way
            query.py caps minimal-mode results at five. Standard mode also
            spends a shared 400-step budget across the returned flows, so
            a codebase with very deep call chains cannot blow the budget
            with a legal flow count.

    Returns:
        Affected flows sorted by criticality; ``truncated`` is set when
        ``max_flows``, the per-detail-level ceiling, or the step budget cut
        the response.
    """
    store, root = _get_store(repo_root)
    try:
        if changed_files is None:
            base = resolve_review_base(root, base)
            changed_files = get_changed_files(root, base)
            if not changed_files:
                changed_files = get_staged_and_unstaged(root)

        if not changed_files:
            return {
                "status": "ok",
                "summary": "No changed files detected.",
                "affected_flows": [],
                "total": 0,
                "truncated": False,
            }

        # Convert to absolute paths for graph lookup. Graph identity uses
        # POSIX separators (#774), so normalize the joined paths.
        abs_files = [normalize_file_path(root / f) for f in changed_files]
        result = _get_affected_flows(store, abs_files)

        total = result["total"]
        flows = result["affected_flows"]
        ceiling = (
            _MAX_AFFECTED_FLOWS_MINIMAL if detail_level == "minimal"
            else _MAX_AFFECTED_FLOWS_STANDARD
        )
        # ``max_flows=0`` keeps its documented "no caller limit" meaning, but
        # the ceiling still applies -- an escape hatch that can return 250k
        # tokens is the bug #849 reported, not a feature.
        limit = ceiling if max_flows <= 0 else min(max_flows, ceiling)
        truncated = total > limit
        flows = flows[:limit]
        if detail_level == "minimal":
            flows = _project(flows, _DETECT_FLOW_FIELDS)
        else:
            flows, steps_cut = _bound_flow_steps(flows)
            truncated = truncated or steps_cut
        out = {
            "status": "ok",
            "summary": (
                f"{total} flow(s) affected by changes "
                f"in {len(changed_files)} file(s)"
                + _shown_of(len(flows), total)
            ),
            "changed_files": changed_files,
            "affected_flows": flows,
            "total": total,
            "truncated": truncated,
        }
        out["_hints"] = generate_hints(
            "get_affected_flows_tool", out, get_session()
        )

        # Context-savings tracking: baseline is every file the agent
        # would need to grep/read to manually trace affected flows.
        _scope_files: set[str] = set(changed_files)
        for _flow in flows:
            for _step in _flow.get("steps", []):
                if _step.get("file_path"):
                    _scope_files.add(_step["file_path"])
        original_tokens = estimate_file_tokens(root, _scope_files)
        _flow_depths = [
            f.get("depth", f.get("node_count", 3)) for f in flows
        ]
        counterfactual = estimate_counterfactual_cost(
            changed_functions=0,
            test_gaps=0,
            tested_functions=0,
            affected_flows=total,
            flow_avg_depth=(
                sum(_flow_depths) / len(_flow_depths)
                if _flow_depths else 0.0
            ),
            trace_depth=0,
            file_tokens=original_tokens,
        )
        attach_context_savings(
            out,
            original_tokens=original_tokens,
            tool="get_affected_flows_tool",
            repo_root=root,
            counterfactual=counterfactual,
        )
        return out
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()


def _unindexed_changed_files(store: Any, abs_files: list[str]) -> list[str]:
    """Return the changed files the graph holds no node for.

    One bounded query rather than a lookup per file: a whole-repo diff would
    otherwise pay a round trip for every path. SQLite caps host parameters at
    999, so the list is chunked.
    """
    if not abs_files:
        return []
    try:
        known: set[str] = set()
        for start in range(0, len(abs_files), 500):
            chunk = abs_files[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = store._conn.execute(
                f"SELECT DISTINCT file_path FROM nodes "
                f"WHERE file_path IN ({placeholders})",
                chunk,
            ).fetchall()
            known.update(r[0] for r in rows)
        return [fp for fp in abs_files if fp not in known]
    except sqlite3.Error:
        # Coverage is advisory; losing it must not fail the review call.
        logger.debug("Could not compute changed-file coverage", exc_info=True)
        return []


def _attach_coverage(
    result: dict[str, Any],
    unindexed: list[str],
    total: int,
    root: Path,
    store: Any,
) -> None:
    """Record how much of the change set the graph covered.

    Always reported, not only when the score is 0: a partially indexed change
    set produces a plausible-looking score over the indexed half, which is the
    harder case to notice.
    """
    if not total:
        return
    covered = total - len(unindexed)
    result["files_in_graph"] = covered
    result["files_not_in_graph"] = len(unindexed)
    if not unindexed:
        return
    result["unindexed_files"] = unindexed[:20]
    branch = None
    try:
        provenance = graph_provenance(str(root)) or {}
        branch = provenance.get("built_on_branch")
    except Exception:  # nosec B110 — advisory only
        logger.debug("Could not read graph provenance", exc_info=True)
    note = unindexed_changes_note(len(unindexed), total, branch)
    result["confidence"] = note
    if covered == 0:
        # Nothing to score. Leaving status "ok" next to risk_score 0.0 made
        # this indistinguishable from a clean review, and the advisory note
        # alone lost to the agent's own judgement about whether a rebuild was
        # worth the cost. "not_ready" is the established way to say the graph
        # cannot answer yet and to name the command that fixes it.
        result["status"] = "not_ready"
        result["reason"] = note
        result["next_tool_suggestions"] = ["build_or_update_graph"]
    result["summary"] = (
        f"{result.get('summary', '')}"
        f"\n  - Graph coverage: {covered} of {total} "
        f"changed file(s) indexed; {note}"
    ).strip()


# ---------------------------------------------------------------------------
# Tool 16: detect_changes  [REVIEW]
# ---------------------------------------------------------------------------


def detect_changes_func(
    base: str = "HEAD~1",
    changed_files: list[str] | None = None,
    include_source: bool = False,
    max_depth: int = 2,
    repo_root: str | None = None,
    detail_level: str = "standard",
    max_results: int = 25,
    max_flows: int = 20,
) -> dict[str, Any]:
    """Detect changes and produce risk-scored review guidance.

    [REVIEW] Primary tool for code review.  Maps git diffs to affected
    functions, flows, communities, and test coverage gaps.  Returns
    priority-ordered review guidance with risk scores.

    Args:
        base: Git ref to diff against (default: HEAD~1).
        changed_files: Explicit list of changed file paths (relative to repo
            root).  Auto-detected from git diff if omitted.
        include_source: If True, include source code snippets for changed
            functions.  Default: False.
        max_depth: Impact radius depth for BFS traversal.  Default: 2.
        repo_root: Repository root path.  Auto-detected if omitted.
        detail_level: Output detail level.  "standard" returns full analysis;
            "minimal" returns only summary, risk_score, changed_file_count,
            test_gap_count, and top 3 review priorities (text only).
            Default: "standard".
        max_results: Maximum changed functions and test gaps to return
            (default 25, capped at 200). ``changed_functions_total`` and
            ``test_gaps_total`` report the untruncated counts.
        max_flows: Maximum affected flows to embed (default 20, capped at
            200). The embedded flows carry per-flow metadata only; use
            get_affected_flows_tool for step detail. See #849.

    Returns:
        Risk-scored analysis with changed functions, affected flows,
        test gaps, and review priorities, plus ``truncated``.
    """
    _validate_positive_int(max_results, "max_results")
    _validate_positive_int(max_flows, "max_flows")

    store, root = _get_store(repo_root)
    try:
        graph_note, not_ready = ensure_graph_current(root, store)
        if not_ready is not None:
            return not_ready

        base = resolve_review_base(root, base)
        # Detect changed files if not provided.
        if changed_files is None:
            changed_files = get_changed_files(root, base)
            if not changed_files:
                changed_files = get_staged_and_unstaged(root)

        if not changed_files:
            return {
                "status": "ok",
                "summary": "No changed files detected.",
                "risk_score": 0.0,
                "changed_functions": [],
                "affected_flows": [],
                "test_gaps": [],
                "review_priorities": [],
            }

        # Convert to absolute paths for graph lookup. Graph identity uses
        # POSIX separators (#774), so normalize the joined paths.
        abs_files = [normalize_file_path(root / f) for f in changed_files]

        # Line ranges are left to analyze_changes, which derives them from
        # the same parse_diff_ranges call and then scopes them to the files
        # named above (#1017). Passing them here instead marked them as
        # caller-supplied scope, so the local working-tree diff *replaced*
        # ``changed_files``: reviewing a remote PR that is not checked out
        # reported the entities of whatever the local checkout was diffing,
        # under the names of the PR's files, with a risk score to match.
        analysis = analyze_changes(
            store,
            changed_files=abs_files,
            changed_ranges=None,
            repo_root=str(root),
            base=base,
        )

        # How much of the change set the graph actually holds. Without this a
        # blind run and a clean run are the same response.
        unindexed = _unindexed_changed_files(store, abs_files)

        # Baseline: tokens an agent would read without the graph —
        # the changed files plus every file referenced in the analysis
        # (function bodies, flow steps, test targets).
        _scope_files: set[str] = set(changed_files)
        for _fn in analysis.get("changed_functions", []):
            if _fn.get("file_path"):
                _scope_files.add(_fn["file_path"])
        for _flow in analysis.get("affected_flows", []):
            for _step in _flow.get("steps", []):
                if _step.get("file_path"):
                    _scope_files.add(_step["file_path"])
        original_tokens = estimate_file_tokens(root, _scope_files)

        # Only diff-backed entries stand in for work a manual reviewer would
        # actually have done. "whole_file" entries (#852) are every
        # function in a file we couldn't get precise ranges for — a
        # reviewer following the actual diff would never have opened most
        # of them, so counting them here would price the graph against a
        # counterfactual nobody would have performed.
        _n_changed_funcs = sum(
            1 for f in analysis.get("changed_functions", [])
            if f.get("scope", "diff") == "diff"
        )
        _n_test_gaps = sum(
            1 for g in analysis.get("test_gaps", [])
            if g.get("scope", "diff") == "diff"
        )
        _affected_flows = analysis.get("affected_flows", [])
        _flow_depths = [
            f.get("depth", f.get("node_count", 3))
            for f in _affected_flows
        ]
        counterfactual = estimate_counterfactual_cost(
            changed_functions=_n_changed_funcs,
            test_gaps=_n_test_gaps,
            tested_functions=max(0, _n_changed_funcs - _n_test_gaps),
            affected_flows=len(_affected_flows),
            flow_avg_depth=(
                sum(_flow_depths) / len(_flow_depths) if _flow_depths else 0.0
            ),
            trace_depth=max_depth,
            file_tokens=original_tokens,
        )

        # Optionally include source snippets for changed functions, spending a
        # shared line budget. Inlining every changed function body turned a
        # whole-repo diff into a 30k-token ``changed_functions`` list.
        if include_source:
            budget = _MAX_DETECT_SOURCE_LINES
            for func in analysis.get("changed_functions", []):
                if budget <= 0:
                    break
                fp = func.get("file_path")
                ls = func.get("line_start")
                le = func.get("line_end")
                if fp and ls and le:
                    file_path = Path(fp)
                    if file_path.is_file():
                        try:
                            lines = file_path.read_text(encoding="utf-8",
                                errors="replace"
                            ).splitlines()
                            start = max(0, ls - 1)
                            end = min(len(lines), le, start + budget)
                            func["source"] = "\n".join(
                                f"{i + 1}: {lines[i]}"
                                for i in range(start, end)
                            )
                            budget -= max(0, end - start)
                        except (OSError, UnicodeDecodeError):
                            func["source"] = "(could not read file)"

        _whole_file_files = analysis.get("whole_file_scope_files", [])

        if detail_level == "minimal":
            priorities = analysis.get("review_priorities", [])
            top_priorities = [
                p.get("name", p.get("qualified_name", ""))
                for p in priorities[:3]
            ]
            result: dict[str, Any] = {
                "status": "ok",
                "summary": analysis.get("summary", ""),
                "risk_score": analysis.get("risk_score", 0.0),
                "changed_file_count": len(changed_files),
                "test_gap_count": len(analysis.get("test_gaps", [])),
                "review_priorities": top_priorities,
            }
            if _whole_file_files:
                # these files had no diff hunks at `base`, so every
                # test gap / review priority attributed to them is "lives in
                # a touched file", not "was actually changed" — the caller
                # should discount them, not treat them as findings.
                result["whole_file_scope_file_count"] = len(_whole_file_files)
            _attach_coverage(result, unindexed, len(abs_files), root, store)
        else:
            funcs, funcs_total, funcs_cut = _bounded(
                analysis.get("changed_functions", []),
                max_results, _MAX_CHANGED_FUNCTIONS,
            )
            gaps, gaps_total, gaps_cut = _bounded(
                analysis.get("test_gaps", []),
                max_results, _MAX_CHANGED_FUNCTIONS,
            )
            flows, flows_total, flows_cut = _bounded(
                analysis.get("affected_flows", []),
                max_flows, _MAX_DETECT_FLOWS,
            )
            files, files_total, files_cut = _bounded(
                changed_files, max_results, _MAX_REVIEW_FILES,
            )
            whole_file_files, whole_file_total, _ = _bounded(
                _whole_file_files, max_results, _MAX_REVIEW_FILES,
            )
            any_cut = funcs_cut or gaps_cut or flows_cut or files_cut
            summary = analysis.get("summary", "")
            if any_cut:
                summary += (
                    "\n  - Response bounded: "
                    f"{len(funcs)} of {funcs_total} changed function(s), "
                    f"{len(gaps)} of {gaps_total} test gap(s), "
                    f"{len(flows)} of {flows_total} flow(s) shown"
                )
            result = {
                "status": "ok",
                **analysis,
                "summary": summary,
                "changed_files": files,
                "changed_file_count": files_total,
                "changed_functions": funcs,
                "changed_functions_total": funcs_total,
                "test_gaps": gaps,
                "test_gaps_total": gaps_total,
                "affected_flows": _project(flows, _DETECT_FLOW_FIELDS),
                "affected_flows_total": flows_total,
                "whole_file_scope_files": whole_file_files,
                "whole_file_scope_files_total": whole_file_total,
                "truncated": any_cut,
            }
            _attach_coverage(result, unindexed, len(abs_files), root, store)
        if graph_note:
            result["summary"] = f"{graph_note}\n{result.get('summary', '')}".strip()
        result["_hints"] = generate_hints(
            "detect_changes_tool", result, get_session()
        )
        attach_context_savings(
            result,
            original_tokens=original_tokens,
            tool="detect_changes_tool",
            repo_root=root,
            counterfactual=counterfactual,
        )
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()

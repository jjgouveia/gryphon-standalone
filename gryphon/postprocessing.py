"""Shared post-build processing pipeline.

After the core Tree-sitter parse (full_build or incremental_update), six
post-processing steps must run to populate derived tables:

1. Resolve evidence-backed bare edge endpoints
2. Compute node signatures
3. Rebuild FTS5 search index (benefits from signatures)
4. Trace execution flows (benefits from resolved endpoints)
5. Detect code communities
6. Refresh embeddings (optional, cloud providers)

Steps run one after the other. They share the store's single SQLite
connection, and a connection does not isolate transactions per thread:
run concurrently, one step's BEGIN/COMMIT interleaves with another's
("cannot rollback - no transaction is active"), the failing step logs a
warning and its edges are silently missing from the graph.

This module extracts that pipeline so every entry point — MCP tool, CLI
commands, and watch mode — produces identical results.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from .graph import GraphStore

logger = logging.getLogger(__name__)


def run_post_processing(
    store: GraphStore,
    *,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
) -> dict[str, Any]:
    """Run all post-build steps on a populated graph.

    Each step is non-fatal: failures are logged and collected as warnings
    so the primary build result is never lost. Steps run sequentially on the
    store's one connection (see the module docstring).

    Args:
        store: An open GraphStore with nodes and edges already populated.

    Returns:
        Dict with keys for each step's result count and a ``warnings``
        list (only present when at least one step failed).
    """
    result: dict[str, Any] = {}
    warnings: list[str] = []

    def _run_wave(steps: list) -> None:
        """Run *steps* in order, merging per-step results and warnings.

        Not concurrently: the steps write through one shared connection.
        """
        for step in steps:
            r, w = step(store)
            result.update(r)
            warnings.extend(w)

    # Bare endpoint resolution, then signatures (both read, then batch write).
    _run_wave([_resolve_bare_endpoints, _compute_signatures])

    # FTS rebuild drops and recreates the virtual table — run alone.
    r, w = _rebuild_fts_index(store)
    result.update(r)
    warnings.extend(w)

    # Flows and communities each use BEGIN IMMEDIATE transactions that
    # conflict under concurrent writes on the same connection, so they
    # run sequentially.
    r, w = _trace_flows(store)
    result.update(r)
    warnings.extend(w)

    r, w = _detect_communities(store)
    result.update(r)
    warnings.extend(w)

    # Wave 3: optional embedding refresh (sequential, may call cloud API)
    r, w = _refresh_embeddings(
        store,
        provider=embedding_provider,
        model=embedding_model,
    )
    result.update(r)
    warnings.extend(w)

    if warnings:
        result["warnings"] = warnings
    return result


# -- Individual steps (private) ------------------------------------------


def _resolve_bare_endpoints(
    store: GraphStore,
) -> tuple[dict[str, Any], list[str]]:
    """Resolve bare and C++ scoped call targets before derived graph steps."""
    try:
        resolved = store.resolve_bare_call_targets()
        resolved += store.resolve_bare_tested_by_sources()
        return {
            "bare_edges_resolved": resolved,
            "cpp_scoped_edges_resolved": store.resolve_cpp_scoped_call_targets(),
        }, []
    except sqlite3.OperationalError as e:
        logger.warning("Call-target resolution failed: %s", e)
        return {}, [
            f"Call-target resolution failed: {type(e).__name__}: {e}",
        ]


def _compute_signatures(
    store: GraphStore,
) -> tuple[dict[str, Any], list[str]]:
    """Compute human-readable signatures for nodes that lack one."""
    try:
        rows = store.get_nodes_without_signature()
        signature_rows: list[tuple[str, int]] = []
        for row in rows:
            node_id, name, kind, params, ret = (
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
            )
            if kind in ("Function", "Test"):
                sig = f"def {name}({params or ''})"
                if ret:
                    sig += f" -> {ret}"
            elif kind == "Class":
                sig = f"class {name}"
            else:
                sig = name
            signature_rows.append((sig[:512], node_id))
        # Single transaction via executemany instead of one autocommitted
        # UPDATE per node (issue #721).
        store.update_node_signatures(signature_rows)
        return {"signatures_computed": len(signature_rows)}, []
    except (sqlite3.OperationalError, TypeError, KeyError) as e:
        logger.warning("Signature computation failed: %s", e)
        return {}, [f"Signature computation failed: {type(e).__name__}: {e}"]


def _rebuild_fts_index(
    store: GraphStore,
) -> tuple[dict[str, Any], list[str]]:
    """Rebuild the FTS5 full-text search index."""
    try:
        from .search import rebuild_fts_index

        fts_count = rebuild_fts_index(store)
        return {"fts_indexed": fts_count}, []
    except (sqlite3.OperationalError, ImportError) as e:
        logger.warning("FTS index rebuild failed: %s", e)
        return {}, [f"FTS index rebuild failed: {type(e).__name__}: {e}"]


def _trace_flows(
    store: GraphStore,
) -> tuple[dict[str, Any], list[str]]:
    """Trace execution flows from entry points."""
    try:
        from .flows import store_flows, trace_flows

        flows = trace_flows(store)
        count = store_flows(store, flows)
        return {"flows_detected": count}, []
    except (sqlite3.OperationalError, ImportError) as e:
        logger.warning("Flow detection failed: %s", e)
        return {}, [f"Flow detection failed: {type(e).__name__}: {e}"]


def _detect_communities(
    store: GraphStore,
) -> tuple[dict[str, Any], list[str]]:
    """Detect code communities via Leiden algorithm or file grouping."""
    try:
        from .communities import detect_communities, store_communities

        comms = detect_communities(store)
        count = store_communities(store, comms)
        return {"communities_detected": count}, []
    except (sqlite3.OperationalError, ImportError) as e:
        logger.warning("Community detection failed: %s", e)
        return {}, [f"Community detection failed: {type(e).__name__}: {e}"]


def _refresh_embeddings(
    store: GraphStore,
    *,
    provider: str | None,
    model: str | None,
) -> tuple[dict[str, Any], list[str]]:
    """Run an explicitly requested embedding refresh without failing a build."""
    if provider is None and model is None:
        return {}, []
    if not provider or not model:
        warning = "Embedding refresh requires both an explicit provider and model."
        logger.warning(warning)
        return {}, [warning]

    try:
        from .embeddings import refresh_embeddings

        refreshed = refresh_embeddings(store, provider=provider, model=model)
        if refreshed is not None:
            return {
                "embeddings_refreshed": refreshed["embedded"],
                "embeddings_purged": refreshed["purged"],
            }, []
        return {}, []
    except Exception as exc:
        logger.warning("Embedding refresh failed: %s", exc)
        return {}, [
            f"Embedding refresh failed: {type(exc).__name__}: {exc}",
        ]

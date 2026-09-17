"""Shared utilities for tool sub-modules."""

from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from ..graph import GraphStore
from ..incremental import find_project_root, get_db_path
from ..parser import normalize_file_path

_PROVENANCE_READ_TIMEOUT_SECONDS = 0.05
_PROVENANCE_GIT_TIMEOUT_SECONDS = 1.0
_HEX_SHA = re.compile(r"[0-9a-fA-F]{40,64}")

# The merge-base of two immutable commits never changes, so results are
# cached per (root, sha pair): provenance runs on every tool call and a
# working tree that sits on another branch would otherwise pay one extra
# git subprocess per call. ``None`` results are not cached so a later
# fetch that deepens a shallow history can still succeed.
_MERGE_BASE_CACHE: dict[tuple[str, str, str], str] = {}
_MERGE_BASE_CACHE_LOCK = threading.Lock()
_MERGE_BASE_CACHE_LIMIT = 128

logger = logging.getLogger(__name__)


def _error_response(
    message: str, status: str = "error", **extra: Any,
) -> dict[str, Any]:
    """Build a standardised error response dict."""
    return {"status": status, "error": message, "summary": message, **extra}


def _read_live_git_head(root: Path) -> str | None:
    """Return the checked-out commit without making provenance mandatory.

    ``head_matches_build`` deliberately compares commits only. It does not
    claim that staged, unstaged, or untracked files are represented by the
    graph, avoiding the misleading ``is_stale=False`` contract from #458.
    """
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(root),
            timeout=_PROVENANCE_GIT_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        logger.debug("Could not read live Git HEAD for graph provenance", exc_info=True)
        return None
    if result.returncode != 0:
        logger.debug("git rev-parse failed while reading graph provenance")
        return None
    head_sha = result.stdout.strip()
    return head_sha or None


def _read_merge_base(root: Path, sha_a: str, sha_b: str) -> str | None:
    """Return the best common ancestor of two commits, or ``None``.

    Both inputs must be full hex SHAs; anything else (refs, expressions,
    dash-prefixed strings) is rejected before reaching git. ``None`` means
    no shared history could be proven: unrelated histories, missing commit
    objects, a shallow clone, or any git failure.
    """
    if not (_HEX_SHA.fullmatch(sha_a) and _HEX_SHA.fullmatch(sha_b)):
        return None
    key = (str(root), sha_a, sha_b)
    with _MERGE_BASE_CACHE_LOCK:
        cached = _MERGE_BASE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            ["git", "merge-base", sha_a, sha_b],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(root),
            timeout=_PROVENANCE_GIT_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    # Criss-cross merges can print several best ancestors, one per line;
    # any single one is a valid merge base for the ancestry comparison.
    first = result.stdout.splitlines()[0].strip() if result.stdout.strip() else ""
    if not _HEX_SHA.fullmatch(first):
        return None
    with _MERGE_BASE_CACHE_LOCK:
        if len(_MERGE_BASE_CACHE) < _MERGE_BASE_CACHE_LIMIT:
            _MERGE_BASE_CACHE[key] = first
    return first


def _classify_build_relation(root: Path, built_sha: str, head_sha: str) -> str:
    """Classify the graph's build commit relative to the checked-out HEAD.

    - ``"same"``: the build commit *is* HEAD.
    - ``"ancestor"``: the build commit is contained in HEAD's history. The
      graph is simply behind and can be topped up incrementally.
    - ``"descendant"``: HEAD is contained in the build's history. The
      checkout is older than the graph.
    - ``"diverged"``: both sides contain commits the other lacks (e.g. the
      graph was built on a branch tip the checkout does not contain).
    - ``"unknown"``: no shared history could be proven.
    """
    if built_sha == head_sha:
        return "same"
    merge_base = _read_merge_base(root, built_sha, head_sha)
    if merge_base is None:
        return "unknown"
    if merge_base == built_sha:
        return "ancestor"
    if merge_base == head_sha:
        return "descendant"
    return "diverged"


def graph_provenance(repo_root: str | None = None) -> dict[str, Any] | None:
    """Return best-effort build metadata for one repository's graph.

    The metadata read is deliberately read-only. Missing, incomplete, or
    unreadable graph databases must never make the enclosing tool call fail.
    """
    try:
        root = _resolve_root(repo_root)
        db_path = get_db_path(root, read_only=True)
        if not db_path.exists():
            return None

        # ``as_uri`` escapes URI-significant path characters before the
        # read-only mode query is appended. It also handles Windows drives.
        database_uri = f"{db_path.resolve().as_uri()}?mode=ro"
        # Provenance is optional and reads only three local metadata rows.
        # Allow a brief commit boundary, but never inherit sqlite3's 5-second
        # default wait when a build or migration holds an exclusive lock.
        connection = sqlite3.connect(
            database_uri,
            uri=True,
            timeout=_PROVENANCE_READ_TIMEOUT_SECONDS,
        )
        try:
            rows = dict(connection.execute(
                "SELECT key, value FROM metadata WHERE key IN "
                "('last_updated', 'git_branch', 'git_head_sha')"
            ).fetchall())
        finally:
            connection.close()

        provenance: dict[str, Any] = {}
        updated_at = rows.get("last_updated")
        if isinstance(updated_at, str) and updated_at:
            provenance["updated_at"] = updated_at
            try:
                built_at = datetime.fromisoformat(updated_at)
                # Match aware timestamps with an aware ``now`` in the same
                # timezone; None preserves the stored naive/local format.
                now = datetime.now(tz=built_at.tzinfo)
                provenance["age_seconds"] = max(
                    0, int((now - built_at).total_seconds()),
                )
            except (OverflowError, TypeError, ValueError):
                # A malformed timestamp only removes the derived age. The raw
                # timestamp and independently valid branch/SHA remain useful.
                pass

        head_sha = rows.get("git_head_sha")
        if isinstance(head_sha, str) and head_sha:
            provenance["built_at_sha"] = head_sha
        if provenance:
            branch = rows.get("git_branch")
            if isinstance(branch, str) and branch:
                provenance["built_on_branch"] = branch
            live_head_sha = _read_live_git_head(root)
            if live_head_sha:
                provenance["head_sha"] = live_head_sha
                if isinstance(head_sha, str) and head_sha:
                    provenance["head_matches_build"] = live_head_sha == head_sha
                    provenance["build_relation"] = _classify_build_relation(
                        root, head_sha, live_head_sha,
                    )
        return provenance or None
    except Exception:
        return None


def with_provenance(result: Any, repo_root: str | None = None) -> Any:
    """Attach a ``_graph`` envelope without changing existing fields."""
    if not isinstance(result, dict) or "_graph" in result:
        return result
    provenance = graph_provenance(repo_root)
    if provenance:
        result["_graph"] = provenance
    return result


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
        from ..incremental import incremental_update
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


def ensure_graph_current(
    root: Path, store: GraphStore,
) -> tuple[str | None, dict[str, Any] | None]:
    """Reconcile a graph whose build commit no longer matches HEAD.

    ``get_minimal_context`` was the only tool that checked this before
    answering; ``detect_changes``, ``get_review_context`` and the impact/query
    tools computed a risk score, coverage note, or blast radius straight off
    whatever the graph happened to hold, silently, when the checkout had
    since moved to a different commit or the graph was built for a different
    PR/branch entirely (the graph still holds *a* node at that file path, so
    presence-only coverage checks like ``_unindexed_changed_files`` do not
    catch it). This is the shared reconciliation step: call it right after
    ``_get_store`` and before reading anything from ``store``.

    A commit mismatch does not by itself mean the graph is stale: the build
    commit may be an ancestor of HEAD, or on a divergent line entirely, and
    either can still be reconciled by diffing the build commit against the
    worktree (``resolve_incremental_base`` decides what is diffable, not the
    relation label). Only a build commit missing from the clone (history
    rewrite or shallow fetch) is genuinely unusable.

    Returns ``(note, not_ready)``:

    - ``(None, None)``: no mismatch, or provenance unavailable — proceed.
    - ``(note, None)``: a mismatch was found. ``store`` now reflects HEAD, or,
      if the refresh itself raised, is unchanged and ``note`` says so either
      way. Proceed, folding ``note`` into the response's summary.
    - ``(None, not_ready)``: the build commit is unreachable in this clone.
      Return ``not_ready`` as the tool's result instead of proceeding.
    """
    from ..incremental import get_changed_files, resolve_incremental_base

    provenance = graph_provenance(str(root))
    if not provenance or provenance.get("head_matches_build") is not False:
        return None, None
    incremental_base = resolve_incremental_base(root, store)
    if incremental_base is None:
        return None, {
            "status": "not_ready",
            "reason": "stale_graph",
            "summary": (
                "The graph was built at a commit that is not available in "
                "this clone (history rewrite or shallow fetch). Rebuild it "
                "before requesting this."
            ),
            "next_tool_suggestions": ["build_or_update_graph"],
        }
    note = _auto_refresh_graph(root, store, incremental_base)
    if note is None:
        pending = get_changed_files(root, incremental_base)
        shown = ", ".join(pending[:5])
        note = (
            "Graph is out of date and auto-refresh failed; "
            f"{len(pending)} file(s) changed since the build are not "
            f"indexed{': ' + shown if shown else ''}."
        )
    return note, None


# Common JS/TS builtin method names filtered from callers_of results.
# "Who calls .map()?" returns hundreds of hits and is never useful.
# These are kept in the graph (callees_of still shows them) but excluded
# when doing reverse call tracing to reduce noise.
_BUILTIN_CALL_NAMES: set[str] = {
    "map", "filter", "reduce", "reduceRight", "forEach", "find", "findIndex",
    "some", "every", "includes", "indexOf", "lastIndexOf",
    "push", "pop", "shift", "unshift", "splice", "slice",
    "concat", "join", "flat", "flatMap", "sort", "reverse", "fill",
    "keys", "values", "entries", "from", "isArray", "of", "at",
    "trim", "trimStart", "trimEnd", "split", "replace", "replaceAll",
    "match", "matchAll", "search", "substring", "substr",
    "toLowerCase", "toUpperCase", "startsWith", "endsWith",
    "padStart", "padEnd", "repeat", "charAt", "charCodeAt",
    "assign", "freeze", "defineProperty", "getOwnPropertyNames",
    "hasOwnProperty", "create", "is", "fromEntries",
    "log", "warn", "error", "info", "debug", "trace", "dir", "table",
    "time", "timeEnd", "assert", "clear", "count",
    "then", "catch", "finally", "resolve", "reject", "all", "allSettled", "race", "any",
    "parse", "stringify",
    "floor", "ceil", "round", "random", "max", "min", "abs", "pow", "sqrt",
    "addEventListener", "removeEventListener", "querySelector", "querySelectorAll",
    "getElementById", "createElement", "appendChild", "removeChild",
    "setAttribute", "getAttribute", "preventDefault", "stopPropagation",
    "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    "toString", "valueOf", "toJSON", "toISOString",
    "getTime", "getFullYear", "now",
    "isNaN", "parseInt", "parseFloat", "toFixed",
    "encodeURIComponent", "decodeURIComponent",
    "call", "apply", "bind", "next",
    "emit", "on", "off", "once",
    "pipe", "write", "read", "end", "close", "destroy",
    "send", "status", "json", "redirect",
    "set", "get", "delete", "has",
    "findUnique", "findFirst", "findMany", "createMany",
    "update", "updateMany", "deleteMany", "upsert",
    "aggregate", "groupBy", "transaction",
    "describe", "it", "test", "expect", "beforeEach", "afterEach",
    "beforeAll", "afterAll", "mock", "spyOn",
    "require", "fetch",
}


def _validate_repo_root(path: "Path | str") -> Path:
    """Validate that a path is a plausible project root.

    Ensures the path is an existing directory that contains a ``.git``,
    ``.svn``, or ``.gryphon`` directory, preventing arbitrary
    file-system traversal via the ``repo_root`` parameter.
    """
    resolved = Path(path).resolve()
    if not resolved.is_dir():
        raise ValueError(
            f"repo_root is not an existing directory: {resolved}"
        )
    has_vcs = (
        (resolved / ".git").exists()
        or (resolved / ".svn").exists()
        or (resolved / ".gryphon").exists()
    )
    if not has_vcs:
        raise ValueError(
            f"repo_root does not look like a project root "
            f"(no .git, .svn, or .gryphon directory found): "
            f"{resolved}"
        )
    return resolved


def _resolve_root(repo_root: str | None = None) -> Path:
    """Resolve and validate the repository root without opening a store."""
    return _validate_repo_root(Path(repo_root)) if repo_root else find_project_root()


def _get_store(repo_root: str | None = None) -> tuple[GraphStore, Path]:
    """Resolve repo root and open the graph store.

    Callers own the returned store and must close it (try/finally or
    context manager) to avoid leaking SQLite file descriptors.
    """
    root = _resolve_root(repo_root)
    db_path = get_db_path(root)
    return GraphStore(db_path), root


def _resolve_graph_file_paths(
    store: GraphStore, root: Path, file_paths: list[str],
) -> list[str]:
    """Resolve user-facing file paths to the paths stored in the graph.

    Graphs may contain absolute paths, repo-relative paths, or cwd-relative
    paths depending on how they were built. Tool inputs are usually relative to
    repo root, so exact matching alone can miss existing graph nodes.
    """
    resolved: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if path not in seen:
            resolved.append(path)
            seen.add(path)

    for file_path in file_paths:
        raw = file_path.replace("\\", "/")
        candidates = [raw]
        path = Path(file_path)
        if path.is_absolute():
            try:
                candidates.append(str(path.resolve().relative_to(root)).replace("\\", "/"))
            except ValueError:
                pass
        else:
            candidates.append(normalize_file_path(root / path))

        for candidate in candidates:
            if store.get_nodes_by_file(candidate):
                add(candidate)

        suffixes = []
        for candidate in candidates:
            normalized = candidate.replace("\\", "/")
            if normalized not in suffixes:
                suffixes.append(normalized)

        for suffix in suffixes:
            for matched_path in store.get_files_matching(suffix):
                add(matched_path)

    return resolved


# ---------------------------------------------------------------------------
# Result bounding (#849 follow-up)
# ---------------------------------------------------------------------------
#
# Every MCP tool response has to survive a client-side context window. #849
# found get_affected_flows returning 247k tokens inside a workflow documented
# as "5 tool calls, 800 tokens total"; PR #853 capped that one tool. These
# helpers give the remaining tools the same contract:
#
#   * ``total`` always reports the untruncated count,
#   * ``truncated`` marks that the list was cut,
#   * the summary line says how many of how many are shown.
#
# Each tool pairs a caller-facing default with a hard ceiling. The ceiling
# exists so a caller passing ``max_results=1_000_000`` still gets a response
# that fits the ~25k-token budget most MCP clients allow for one tool result.


def _validate_positive_int(value: int, name: str) -> int:
    """Validate a caller-supplied result bound.

    Mirrors the check ``query.py`` applies to ``max_results``: ``bool`` is
    rejected explicitly because ``True`` would otherwise silently mean 1.
    """
    if isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be an integer greater than or equal to 1")
    return value


def _bounded(
    items: "list[Any]", max_results: int, hard_cap: int,
) -> tuple[list[Any], int, bool]:
    """Cap *items* at ``min(max_results, hard_cap)``.

    Returns ``(visible, total, truncated)`` where ``total`` is the
    untruncated length, so callers can always report the real count.
    """
    total = len(items)
    limit = min(max_results, hard_cap)
    return list(items[:limit]), total, total > limit


def _shown_of(shown: int, total: int) -> str:
    """Return the ``", showing N of M"`` fragment used by capped summaries."""
    return f", showing {shown} of {total}" if shown < total else ""


def compact_response(
    summary: str,
    key_entities: list[str] | None = None,
    risk: str = "unknown",
    communities: list[str] | None = None,
    flows_affected: list[str] | None = None,
    next_tool_suggestions: list[str] | None = None,
    data: dict[str, Any] | None = None,
    detail_level: str = "minimal",
) -> dict[str, Any]:
    """Standard compact response format for token efficiency."""
    resp: dict[str, Any] = {
        "status": "ok",
        "summary": summary,
    }
    if key_entities:
        resp["key_entities"] = key_entities[:10]
    if risk != "unknown":
        resp["risk"] = risk
    if communities:
        resp["communities"] = communities[:5]
    if flows_affected:
        resp["flows_affected"] = flows_affected[:5]
    if next_tool_suggestions:
        resp["next_tool_suggestions"] = next_tool_suggestions[:3]
    if detail_level != "minimal" and data:
        resp["data"] = data
    return resp

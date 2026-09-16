"""Measure real token savings of the graph for a concrete diff.

Baseline is the counterfactual chosen for the savings dashboard: the tokens
an agent would read without the graph, approximated as the full content of
the changed files plus every impacted file the graph surfaces. The graph
cost is the serialized minimal impact response — what the agent actually
consumes instead.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Iterable

from .context_savings import estimate_tokens
from .incremental import (
    get_changed_files,
    get_staged_and_unstaged,
    resolve_review_base,
)
from .savings_log import log_savings
from .tools._common import _get_store, _resolve_graph_file_paths

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 30


def _tokenize(text: str, enc: Any | None) -> int:
    if enc is not None:
        return len(enc.encode(text))
    return estimate_tokens(text)


def _load_tiktoken():
    try:
        import tiktoken  # type: ignore[import-untyped]

        return tiktoken.get_encoding("cl100k_base")
    except ImportError:
        return None


def _files_content_tokens(
    root: Path, files: Iterable[str], enc: Any | None
) -> tuple[int, list[str]]:
    """Tokenize file contents; returns (total_tokens, files_read)."""
    total = 0
    read: list[str] = []
    for name in files:
        path = Path(name)
        full = path if path.is_absolute() else root / path
        try:
            if full.is_file():
                total += _tokenize(full.read_text(errors="replace"), enc)
                read.append(name)
        except OSError:
            continue
    return total, read


def _diff_files(root: Path, base: str, head: str) -> list[str]:
    """`git diff --name-only base...head`, for measuring a committed range."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}", "--"],
        capture_output=True,
        cwd=str(root),
        timeout=_GIT_TIMEOUT,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git diff {base}...{head} failed: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    return [
        line.strip()
        for line in result.stdout.decode(errors="replace").splitlines()
        if line.strip()
    ]


def measure_savings(
    repo_root: str | None = None,
    *,
    changed_files: list[str] | None = None,
    base: str = "HEAD~1",
    head: str | None = None,
    max_depth: int = 2,
    max_results: int = 500,
    ref: str | None = None,
    log: bool = True,
) -> dict[str, Any]:
    """Measure token savings for one diff.

    Args:
        repo_root: Repository root (auto-detected when omitted).
        changed_files: Explicit changed-file list. When omitted, resolves
            ``base...head`` via ``git diff --name-only`` when *head* is given,
            otherwise falls back to the review base / working-tree detection
            used by the impact tool.
        base: Git base ref (default ``HEAD~1``).
        head: Optional head ref; enables ``base...head`` range diffing.
        max_depth: Blast-radius traversal depth.
        max_results: Node cap passed to the graph impact query.
        ref: Free-form label stored in the log entry (e.g. ``PR #197``).
        log: Append a ``kind="measure"`` entry to the savings log.

    Returns:
        Dict with baseline/graph/saved token counts, the file lists used,
        and ``verified=True`` when tiktoken did the counting.
    """
    enc = _load_tiktoken()
    store, root = _get_store(repo_root)
    try:
        if changed_files is None:
            if head:
                changed_files = _diff_files(root, base, head)
                ref = ref or f"{base}...{head}"
            else:
                base = resolve_review_base(root, base)
                changed_files = get_changed_files(root, base)
                if not changed_files:
                    changed_files = get_staged_and_unstaged(root)
                ref = ref or f"{base} → working tree"

        if not changed_files:
            return {
                "status": "ok",
                "summary": "No changed files detected.",
                "changed_files": [],
                "impacted_files": [],
                "baseline_tokens": 0,
                "graph_tokens": 0,
                "saved_tokens": 0,
                "saved_percent": 0,
            }

        abs_files = _resolve_graph_file_paths(store, root, changed_files)
        impact = store.get_impact_radius(
            abs_files, max_depth=max_depth, max_nodes=max_results
        )
        impacted_files = list(impact["impacted_files"])
        total_impacted = impact["total_impacted"]
        impacted_dicts = impact["impacted_nodes"]

        changed_tokens, _ = _files_content_tokens(root, changed_files, enc)
        impacted_tokens, impacted_read = _files_content_tokens(
            root, impacted_files, enc
        )

        # The minimal tool response is what an agent consumes instead of the
        # naive read. Mirror get_impact_radius's minimal shape (summary,
        # risk, counts, key entities — no node payloads) so the cost matches
        # what a tool call would actually return.
        impacted_count = len(impacted_dicts)
        if impacted_count > 20:
            risk = "high"
        elif impacted_count > 5:
            risk = "medium"
        else:
            risk = "low"
        minimal_response = {
            "status": "ok",
            "summary": (
                f"Blast radius for {len(changed_files)} changed file(s): "
                f"{len(impact['changed_nodes'])} nodes directly changed, "
                f"{impacted_count} nodes impacted (within {max_depth} hops), "
                f"{len(impacted_files)} additional files affected"
            ),
            "risk": risk,
            "impacted_file_count": len(impacted_files),
            "key_entities": [n.name for n in impacted_dicts[:5]],
            "truncated": impact["truncated"],
            "nodes_omitted": max(0, total_impacted - impacted_count),
        }
        graph_tokens = _tokenize(
            json.dumps(
                minimal_response, default=str, ensure_ascii=False,
                separators=(",", ":"),
            ),
            enc,
        )

        baseline = changed_tokens + impacted_tokens
        returned = changed_tokens + graph_tokens
        saved = max(0, baseline - returned)
        percent = round(saved * 100 / baseline) if baseline > 0 else 0

        result: dict[str, Any] = {
            "status": "ok",
            "ref": ref,
            "changed_files": changed_files,
            "impacted_files": impacted_files,
            "changed_tokens": changed_tokens,
            "impacted_tokens": impacted_tokens,
            "baseline_tokens": baseline,
            "returned_tokens": returned,
            "graph_tokens": graph_tokens,
            "saved_tokens": saved,
            "saved_percent": percent,
            "verified": enc is not None,
            "impacted_files_read": len(impacted_read),
        }

        if log and baseline > 0:
            try:
                log_savings(
                    root,
                    kind="measure",
                    ref=ref,
                    baseline_tokens=baseline,
                    returned_tokens=returned,
                    saved_tokens=saved,
                    saved_percent=percent,
                    estimated=enc is None,
                    extra={
                        "changed_files": len(changed_files),
                        "impacted_files": len(impacted_files),
                    },
                )
            except Exception:  # noqa: BLE001 - measurement must not fail on logging
                logger.debug("savings log write failed", exc_info=True)

        return result
    finally:
        store.close()

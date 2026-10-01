"""Persistent JSONL log of estimated token savings.

Two sinks mirror each other: a per-repo ``.gryphon/savings.jsonl`` (for
repo-local inspection) and a global ``$CRG_HOME/savings.jsonl`` (the source
the dashboard aggregates across repositories). Every write is best-effort —
a read-only or missing directory must never break a tool call.
"""

from __future__ import annotations

import json
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .constants import crg_home

LOG_NAME = "savings.jsonl"

_LOCK = threading.Lock()


def _repo_log_path(repo_root: Path) -> Path:
    # The per-repo log lives next to the graph: registry entry, then
    # CRG_DATA_DIR, then <repo>/.gryphon. A hard-coded .gryphon wrote into
    # the working tree of repos whose graph is kept elsewhere, and without
    # the data dir's .gitignore the folder showed up in `git status`.
    from .incremental import get_data_dir

    return get_data_dir(repo_root) / LOG_NAME


def _global_log_path() -> Path:
    return crg_home() / LOG_NAME


def _append_line(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def log_savings(
    repo_root: "Path | str",
    *,
    kind: str,
    baseline_tokens: int,
    returned_tokens: int,
    saved_tokens: int,
    saved_percent: int,
    tool: str | None = None,
    estimated: bool = True,
    ref: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one savings entry to the per-repo and global JSONL logs.

    ``kind`` distinguishes provenance: ``tool_call`` for automatic entries
    emitted by ``attach_context_savings`` and ``measure`` for on-demand
    diff measurements.
    """
    root = Path(repo_root)
    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo": str(root),
        "kind": kind,
        "tool": tool,
        "baseline_tokens": int(baseline_tokens),
        "returned_tokens": int(returned_tokens),
        "saved_tokens": int(saved_tokens),
        "saved_percent": int(saved_percent),
        "estimated": bool(estimated),
    }
    if ref:
        entry["ref"] = ref
    if extra:
        entry["extra"] = extra
    with _LOCK:
        for path in {_repo_log_path(root), _global_log_path()}:
            try:
                _append_line(path, entry)
            except OSError:
                continue
    return entry


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return entries


def read_entries(repo_root: "Path | str | None" = None) -> list[dict[str, Any]]:
    """Read logged entries — global log by default, per-repo when given."""
    if repo_root is not None:
        return _read_jsonl(_repo_log_path(Path(repo_root)))
    return _read_jsonl(_global_log_path())


def summarize(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate entries into totals, per-day, per-tool and per-repo views."""
    entries = list(entries)
    total_baseline = sum(e.get("baseline_tokens", 0) for e in entries)
    total_returned = sum(e.get("returned_tokens", 0) for e in entries)
    total_saved = sum(e.get("saved_tokens", 0) for e in entries)

    by_day: dict[str, int] = defaultdict(int)
    by_tool: Counter[str] = Counter()
    by_repo: Counter[str] = Counter()

    # Counterfactual aggregation (backward-compatible: missing fields → 0).
    total_cf_saved = 0
    total_cf_baseline = 0
    cf_by_day: dict[str, int] = defaultdict(int)
    cf_by_dimension: dict[str, int] = defaultdict(int)

    for e in entries:
        day = str(e.get("ts", ""))[:10]
        if day:
            by_day[day] += e.get("saved_tokens", 0)
        if e.get("tool"):
            by_tool[e["tool"]] += e.get("saved_tokens", 0)
        if e.get("repo"):
            by_repo[e["repo"]] += e.get("saved_tokens", 0)

        extra = e.get("extra") or {}
        cf = extra.get("counterfactual")
        if cf:
            cf_baseline = cf.get("total_counterfactual", 0)
            cf_saved = extra.get("total_saved_tokens", 0)
            total_cf_baseline += cf_baseline
            total_cf_saved += cf_saved
            if day:
                cf_by_day[day] += cf_saved
            for dim in (
                "search_trace_tokens", "analysis_tokens",
                "precision_tokens", "file_tokens",
            ):
                cf_by_dimension[dim] += cf.get(dim, 0)

    return {
        "count": len(entries),
        "total_baseline_tokens": total_baseline,
        "total_returned_tokens": total_returned,
        "total_saved_tokens": total_saved,
        "total_saved_percent": (
            round(total_saved * 100 / total_baseline) if total_baseline > 0 else 0
        ),
        "by_day": dict(sorted(by_day.items())),
        "by_tool": dict(by_tool.most_common()),
        "by_repo": dict(by_repo.most_common()),
        "cf_total_baseline": total_cf_baseline,
        "cf_total_saved": total_cf_saved,
        "cf_total_saved_percent": (
            round(total_cf_saved * 100 / total_cf_baseline)
            if total_cf_baseline > 0 else 0
        ),
        "cf_by_day": dict(sorted(cf_by_day.items())),
        "cf_by_dimension": dict(cf_by_dimension),
    }

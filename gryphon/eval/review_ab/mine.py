"""Ground-truth candidates for a case with SZZ: later fixes blamed back to the PR.

For every commit after the PR's merge whose subject looks like a fix, the
lines the fix removed or changed are blamed on the fix's parent. Lines born
in one of the PR's own commits make the fix a candidate known issue.

Candidates are for a human to curate, never ground truth as-is: SZZ also
flags refactors and cosmetic edits labelled "fix", and it cannot see fixes
that only add code (a missing check has no old line to blame).
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from .cases import ReviewCase

logger = logging.getLogger(__name__)

FIX_SUBJECT_RE = re.compile(r"\b(fix|fixes|fixed|hotfix|revert|bug|corrig\w*|conserta\w*)\b", re.I)
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    ).stdout


def pr_commits(repo: Path, case: ReviewCase) -> set[str]:
    """Commits the PR brought in: reachable from head, not from base."""
    out = _git(repo, "rev-list", f"{case.base_sha}..{case.head_sha}")
    return set(out.split())


def merge_time(repo: Path, case: ReviewCase) -> int:
    """Commit time of the PR head; fixes must come after it."""
    return int(_git(repo, "show", "-s", "--format=%ct", case.head_sha).strip())


def fix_commits(repo: Path, since: int, exclude: set[str]) -> list[tuple[str, str]]:
    """``(sha, subject)`` of fix-looking non-merge commits not older than *since*.

    Callers pass the head's history in *exclude*: ancestors of the head cannot
    fix what the head introduced, whatever their timestamp says.
    """
    out = _git(repo, "log", "--all", "--no-merges", "--format=%H%x09%ct%x09%s")
    fixes = []
    for line in out.splitlines():
        sha, ts, subject = (line.split("\t", 2) + ["", ""])[:3]
        if not ts.isdigit() or int(ts) < since:
            continue
        if sha in exclude or not FIX_SUBJECT_RE.search(subject):
            continue
        fixes.append((sha, subject))
    return fixes


def removed_ranges(repo: Path, sha: str) -> dict[str, list[tuple[int, int]]]:
    """Old-side line ranges each file lost or changed in commit *sha*."""
    diff = _git(repo, "diff", "-U0", "--no-color", "--no-renames", f"{sha}^", sha)
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("--- "):
            path = line[4:]
            current = path[2:] if path.startswith("a/") else None
        elif line.startswith("@@") and current:
            m = _HUNK_RE.match(line)
            if not m:
                continue
            start, count = int(m.group(1)), int(m.group(2) or "1")
            if count > 0:
                ranges.setdefault(current, []).append((start, start + count - 1))
    return ranges


def blame_origins(repo: Path, sha: str, path: str, start: int, end: int) -> set[str]:
    """Commits that last touched lines start..end of *path* before *sha*."""
    try:
        out = _git(repo, "blame", "--porcelain", "-L", f"{start},{end}", f"{sha}^", "--", path)
    except subprocess.CalledProcessError as exc:
        logger.warning("blame failed for %s %s:%d-%d: %s", sha[:8], path, start, end, exc)
        return set()
    return {
        line.split()[0] for line in out.splitlines()
        if re.match(r"^[0-9a-f]{40} \d+ \d+", line)
    }


def mine_case(case: ReviewCase, *, max_fixes: int = 200) -> list[dict]:
    """Candidate known issues for *case*, one per fix commit that blames the PR."""
    repo = Path(case.source_repo)
    introduced = pr_commits(repo, case)
    if not introduced:
        return []
    candidates = []
    history = set(_git(repo, "rev-list", case.head_sha).split())
    fixes = fix_commits(repo, merge_time(repo, case), exclude=history)
    for sha, subject in fixes[:max_fixes]:
        hits: list[dict] = []
        for path, spans in removed_ranges(repo, sha).items():
            for start, end in spans:
                origins = blame_origins(repo, sha, path, start, end) & introduced
                if origins:
                    hits.append({
                        "file": path, "lines": f"{start}-{end}",
                        "introduced_by": sorted(o[:12] for o in origins),
                    })
        if hits:
            candidates.append({
                "id": f"K-{sha[:8]}",
                "fix_commit": sha,
                "fix_subject": subject,
                "file": hits[0]["file"],
                "description": subject,
                "blamed_lines": hits,
                "source": "szz",
                "confirmed": False,
            })
    return candidates

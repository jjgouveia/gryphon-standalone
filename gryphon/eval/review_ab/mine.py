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


def _git(repo: Path, *args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True, input=stdin,
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
    try:
        diff = _git(repo, "diff", "-U0", "--no-color", "--no-renames", f"{sha}^", sha)
    except subprocess.CalledProcessError as exc:
        # A root commit, or the edge of a shallow clone: no parent to diff.
        logger.warning("no parent diff for %s: %s", sha[:8], (exc.stderr or "").strip()[:120])
        return {}
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


_MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+)\b")


def merged_prs(
    repo: Path, refs: list[str], *, merge_shas: dict[str, int] | None = None,
) -> list[dict]:
    """PRs merged anywhere in *refs*: number, merge SHA, base, head, commits.

    Without *merge_shas*, a PR is a merge commit whose message starts with
    "Merge pull request #N" and its commits are those reachable from the
    second parent and not from the first. All branches count, not only one
    first-parent line: in a feature -> homologation -> main flow, feature PRs
    never reach main's first-parent history.

    With *merge_shas* (SHA -> number, from the host), a PR is identified by
    its SHA whatever the message says, and squash merges count too: a
    single-parent commit the host reports as a PR's merge is that PR, with
    itself as its only commit.
    """
    out = _git(repo, "log", "--merges", *refs, "--format=%H%x09%P%x09%ct%x09%s")
    prs = []
    seen: set[str] = set()
    for line in out.splitlines():
        sha, parents, ts, subject = (line.split("\t", 3) + ["", "", ""])[:4]
        parent_list = parents.split()
        if len(parent_list) != 2 or sha in seen:
            continue
        if merge_shas is not None:
            number = merge_shas.get(sha)
        else:
            m = _MERGE_PR_RE.match(subject)
            number = int(m.group(1)) if m else None
        if number is None:
            continue
        seen.add(sha)
        first, second = parent_list
        commits = set(_git(repo, "rev-list", f"{first}..{second}").split())
        prs.append({
            "pr": number, "merge_sha": sha, "base_sha": first, "head_sha": second,
            "merged_at": int(ts), "subject": subject, "commits": commits,
        })
    if merge_shas:
        prs += _squash_prs(repo, {s: n for s, n in merge_shas.items() if s not in seen})
    return prs


def _squash_prs(repo: Path, candidates: dict[str, int]) -> list[dict]:
    """Single-parent commits among *candidates* that exist in the clone."""
    if not candidates:
        return []
    check = _git(repo, "cat-file", "--batch-check", stdin="\n".join(candidates) + "\n")
    present = [line.split()[0] for line in check.splitlines() if line.endswith(" commit")
               or " commit " in line]
    if not present:
        return []
    out = _git(repo, "log", "--no-walk=unsorted", "--stdin", "--format=%H%x09%P%x09%ct%x09%s",
               stdin="\n".join(present) + "\n")
    prs = []
    for line in out.splitlines():
        sha, parents, ts, subject = (line.split("\t", 3) + ["", "", ""])[:4]
        parent_list = parents.split()
        if len(parent_list) != 1:
            continue
        prs.append({
            "pr": candidates[sha], "merge_sha": sha, "base_sha": parent_list[0],
            "head_sha": sha, "merged_at": int(ts), "subject": subject, "commits": {sha},
        })
    return prs


def github_merge_shas(gh_repo: str) -> dict[str, int]:
    """Merge commit SHA -> PR number for every merged PR of *gh_repo*.

    A clone can carry merges of another repository's history (a repo that
    started as a copy of an older one), numbered in that repository; only a
    merge SHA the host reports as a PR of *gh_repo* identifies one of its PRs.
    """
    result = subprocess.run(
        ["gh", "pr", "list", "--repo", gh_repo, "--state", "merged", "--limit", "5000",
         "--json", "number,mergeCommit"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    import json

    return {
        pr["mergeCommit"]["oid"]: pr["number"]
        for pr in json.loads(result.stdout)
        if pr.get("mergeCommit")
    }


def mine_repo(
    repo: Path, refs: list[str], *, max_fixes: int = 400,
    merge_shas: dict[str, int] | None = None,
) -> list[dict]:
    """PRs merged in *refs* ranked by later fixes blamed back to their commits.

    The reverse of :func:`mine_case`: start from every fix-looking commit in
    *refs*, blame the lines it removed or changed, and credit the PR whose
    commits introduced them. A fix never counts against its own PR. A commit
    carried by several PRs (a feature PR, then the promotion PR that moved it
    to main) belongs to the smallest one: the feature PR that wrote it.
    *merge_shas* (see :func:`github_merge_shas`) keeps only the merges that
    are PRs of the repository on the host, numbered as the host numbers them.
    """
    prs = merged_prs(repo, refs, merge_shas=merge_shas)
    owner: dict[str, int] = {}
    size: dict[str, int] = {}
    for pr in prs:
        for c in pr["commits"]:
            if c not in owner or len(pr["commits"]) < size[c]:
                owner[c], size[c] = pr["pr"], len(pr["commits"])
    by_number: dict[int, dict] = {}
    for pr in prs:
        by_number.setdefault(pr["pr"], pr)
    history = _git(repo, "log", "--no-merges", *refs, "--format=%H%x09%s").splitlines()
    fixes = [
        (sha, subject) for sha, _, subject in (line.partition("\t") for line in history)
        if FIX_SUBJECT_RE.search(subject)
    ][:max_fixes]

    found: dict[int, list[dict]] = {}
    for sha, subject in fixes:
        own_pr = owner.get(sha)
        blamed: dict[int, list[dict]] = {}
        for path, spans in removed_ranges(repo, sha).items():
            for start, end in spans:
                for origin in blame_origins(repo, sha, path, start, end):
                    pr_number = owner.get(origin)
                    if pr_number is None or pr_number == own_pr:
                        continue
                    blamed.setdefault(pr_number, []).append(
                        {"file": path, "lines": f"{start}-{end}"}
                    )
        for pr_number, hits in blamed.items():
            found.setdefault(pr_number, []).append({
                "id": f"K-{sha[:8]}",
                "fix_commit": sha,
                "fix_pr": own_pr,
                "fix_subject": subject,
                "file": hits[0]["file"],
                "description": subject,
                "blamed_lines": hits,
                "source": "szz",
                "confirmed": False,
            })
    ranked = [
        {**{k: v for k, v in by_number[pr].items() if k != "commits"},
         "changed_commits": len(by_number[pr]["commits"]), "known_issues": issues}
        for pr, issues in found.items()
    ]
    ranked.sort(key=lambda r: (-len(r["known_issues"]), -r["merged_at"]))
    return ranked

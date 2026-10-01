"""Review cases: a closed pull request pinned to its base and head commits."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ReviewCase:
    """One pull request to review.

    ``base_sha`` is the base branch commit the PR merged into (first parent
    of the merge commit); the reviewed change is ``git diff base...head``.
    ``known_issues`` is the ground truth used by the judging pass.
    """

    id: str
    source_repo: str
    base_sha: str
    head_sha: str
    title: str
    body: str = ""
    gh_repo: str = ""
    pr: int | None = None
    known_issues: list[dict] = field(default_factory=list)

    def confirmed_issues(self) -> list[dict]:
        """Known issues a human confirmed; SZZ candidates start unconfirmed."""
        return [k for k in self.known_issues if k.get("confirmed", True)]

    def __post_init__(self) -> None:
        if not _CASE_ID_RE.match(self.id):
            raise ValueError(f"invalid case id {self.id!r}")
        for name in ("base_sha", "head_sha"):
            if not _SHA_RE.match(getattr(self, name)):
                raise ValueError(f"case {self.id}: {name} must be a full 40-char SHA")


def _require_yaml() -> None:
    if yaml is None:
        raise ImportError(
            "pyyaml is required: pip install "
            "'gryphon[eval] @ git+https://github.com/jjgouveia/gryphon-standalone.git'"
        )


def load_cases(path: Path) -> list[ReviewCase]:
    """Load cases from a YAML file holding a ``cases:`` list."""
    _require_yaml()
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    cases = [ReviewCase(**raw) for raw in data.get("cases", [])]
    ids = [c.id for c in cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"{path}: duplicate case ids {duplicates}")
    return cases


def save_cases(cases: list[ReviewCase], path: Path) -> None:
    """Write cases back to YAML, keeping insertion order."""
    _require_yaml()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"cases": [asdict(c) for c in cases]}
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)


def upsert_case(case: ReviewCase, path: Path) -> None:
    """Add *case* to the file at *path*, replacing a case with the same id."""
    cases = load_cases(path) if path.exists() else []
    cases = [c for c in cases if c.id != case.id] + [case]
    save_cases(cases, path)


def case_from_github(gh_repo: str, pr: int, source_repo: Path) -> ReviewCase:
    """Build a case from a merged GitHub PR via ``gh``.

    The base is the merge commit's first parent, resolved in *source_repo*,
    so the sanitized clone never needs the merge commit itself.
    """
    result = subprocess.run(
        [
            "gh", "pr", "view", str(pr), "--repo", gh_repo,
            "--json", "number,title,body,headRefOid,baseRefOid,mergeCommit,state",
        ],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    meta = json.loads(result.stdout)
    if meta.get("state") != "MERGED" or not meta.get("mergeCommit"):
        raise ValueError(f"{gh_repo}#{pr} is not merged; only merged PRs have a fixed base")
    merge_sha = meta["mergeCommit"]["oid"]
    parent = subprocess.run(
        ["git", "rev-parse", f"{merge_sha}^1"],
        cwd=str(source_repo), capture_output=True, text=True, check=True,
    )
    head = meta["headRefOid"]
    head_known = subprocess.run(
        ["git", "cat-file", "-e", f"{head}^{{commit}}"],
        cwd=str(source_repo), capture_output=True,
    ).returncode == 0
    if not head_known:
        # A squash-merged PR whose branch is gone: the squash commit holds the
        # same change against the same parent.
        head = merge_sha
    repo_slug = gh_repo.rsplit("/", 1)[-1]
    return ReviewCase(
        id=f"{repo_slug}-{pr}",
        source_repo=str(source_repo.resolve()),
        base_sha=parent.stdout.strip(),
        head_sha=head,
        title=meta.get("title") or "",
        body=meta.get("body") or "",
        gh_repo=gh_repo,
        pr=pr,
    )

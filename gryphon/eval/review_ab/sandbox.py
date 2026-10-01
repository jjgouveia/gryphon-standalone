"""Sanitized per-arm clones of a review case.

A clone holds only the history reachable from the PR head and its base, so the
reviewer cannot find later fix commits with ``git log``. Agent configuration
(``.claude/``, ``.mcp.json``, other platforms' rule folders) is kept out of the
working tree with a sparse checkout, which leaves ``git status`` clean. The
graph arm's database and ``CRG_HOME`` live outside the clone so the user's
global registry and savings log are never touched.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .cases import ReviewCase

logger = logging.getLogger(__name__)

# baseline: no graph. graph: gryphon as ``gryphon install`` sets it up, the
# reviewer decides whether to call it. graph_required: same setup, and the
# prompt makes the graph calls mandatory, so the technique is measured even
# when the reviewer would not have chosen it.
#
# Adoption ablation arms (need isolation="project": --restricted does not load
# CLAUDE.md). graph_md: the install block as a real CLAUDE.md in the clone
# instead of an appended system prompt. graph_md_enrich: the same plus the
# ``gryphon enrich`` PreToolUse hook, which injects graph context into Grep,
# Glob, Bash and Read calls without the reviewer asking.
# graph_install: exactly what `gryphon install` sets up for Claude Code today
# (skills.generate_hooks_config verbatim + the CLAUDE.md block), so it follows
# every change to the install instead of a copy of it.
# graph_install_ref: the same, from another gryphon checkout (``--ref-python``):
# its hooks, its CLAUDE.md block, its MCP server and its `gryphon` on PATH.
# Two versions of the install compete in one run and one blind judgment.
ARMS = (
    "baseline", "graph", "graph_required", "graph_md", "graph_md_enrich", "graph_install",
    "graph_install_ref",
)
GRAPH_ARMS = frozenset(ARMS) - {"baseline"}
CLAUDE_MD_ARMS = frozenset({"graph_md", "graph_md_enrich", "graph_install", "graph_install_ref"})
REF_ARMS = frozenset({"graph_install_ref"})


def sandbox_kind(arm: str) -> str:
    """Arms with the graph share one clone and one graph build per case."""
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {ARMS}")
    return "graph" if arm in GRAPH_ARMS else "baseline"

# Kept out of the working tree in both arms. Instruction files the repo
# documents its own conventions in (CLAUDE.md, AGENTS.md) stay: both arms see
# them, and contamination_warnings flags any that mention the graph.
EXCLUDED_PATHS = (
    ".claude/", ".mcp.json", ".gryphon/", ".code-review-graph/", ".cursor/",
    ".codex/", ".gemini/", ".kiro/", ".qoder/", ".codebuddy/", ".windsurf/",
    ".continue/", ".opencode/", ".serena/", ".vscode/mcp.json",
)

_INSTRUCTION_FILE_RE = re.compile(r"(^|/)(CLAUDE|AGENTS|GEMINI|QODER)(\.[\w-]+)?\.md$", re.I)
_GRAPH_MENTION_RE = re.compile(r"gryphon|code-review-graph", re.I)

class NoMergeBaseError(ValueError):
    """The PR's base and head have no common commit in the source clone."""


REVIEW_BRANCH = "review"
BASE_BRANCH = "base"
_READY_MARKER = ".rab-ready"


@dataclass
class ArmSandbox:
    """A prepared clone of one case: ``kind`` is ``baseline`` or ``graph``."""

    case_id: str
    kind: str
    root: Path
    repo: Path
    data_dir: Path | None = None
    crg_home: Path | None = None
    graph_build_seconds: float | None = None
    contamination_warnings: list[str] = field(default_factory=list)

    def gryphon_env(self) -> dict[str, str]:
        """Environment for gryphon processes serving this sandbox."""
        if self.data_dir is None or self.crg_home is None:
            return {}
        return {"CRG_DATA_DIR": str(self.data_dir), "CRG_HOME": str(self.crg_home)}


def default_workdir() -> Path:
    """Neutral location for clones: nothing in the path names the tool."""
    return Path(tempfile.gettempdir()) / "rab"


def _opaque_name(case_id: str, arm: str) -> str:
    # The baseline agent can list its parent directories from Bash; opaque
    # names keep "graph" and the case id out of what it can see.
    return hashlib.sha1(f"{case_id}:{arm}".encode(), usedforsecurity=False).hexdigest()[:12]


def _clear_readonly(func, path, _exc) -> None:
    # Git writes pack and object files read-only; Windows refuses to delete them.
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_clear_readonly)
    else:
        shutil.rmtree(path, onerror=_clear_readonly)


def _git(args: list[str], cwd: Path, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True, **kwargs,
    )


def _clone(case: ReviewCase, repo: Path) -> None:
    repo.mkdir(parents=True)
    _git(["init", "-q"], repo)
    # Fetching bare SHAs from a local repo needs allowAnySHA1InWant on the
    # serving side; passing it through --upload-pack leaves the source
    # repository's config untouched. --update-shallow accepts shallow sources.
    _git(
        [
            "-c", "protocol.file.allow=always", "fetch", "-q", "--update-shallow",
            "--upload-pack", "git -c uploadpack.allowAnySHA1InWant=true upload-pack",
            str(Path(case.source_repo)),
            f"{case.head_sha}:refs/heads/{REVIEW_BRANCH}",
            f"{case.base_sha}:refs/heads/{BASE_BRANCH}",
        ],
        repo,
    )
    # FETCH_HEAD records the source path, which would point the agent at a
    # checkout that contains the future.
    (repo / ".git" / "FETCH_HEAD").unlink(missing_ok=True)
    sparse = repo / ".git" / "info" / "sparse-checkout"
    sparse.parent.mkdir(parents=True, exist_ok=True)
    sparse.write_text(
        "/*\n" + "".join(f"!/{p}\n" for p in EXCLUDED_PATHS), encoding="utf-8",
    )
    _git(["config", "core.sparseCheckout", "true"], repo)
    _git(["checkout", "-q", REVIEW_BRANCH], repo)
    has_base = subprocess.run(
        ["git", "merge-base", BASE_BRANCH, REVIEW_BRANCH], cwd=str(repo), capture_output=True,
    ).returncode == 0
    if not has_base:
        # `git diff base...review` needs the fork point. A shallow source
        # clone can stop before it; say so instead of failing later on git.
        raise NoMergeBaseError(
            f"{case.id}: base and head share no commit in {case.source_repo}; the clone is"
            " probably shallow past the PR's fork point (git fetch --deepen=N there)"
        )


def contamination_warnings(repo: Path) -> list[str]:
    """Instruction files in the checkout that mention the graph tool."""
    listed = _git(["ls-files"], repo).stdout.splitlines()
    warnings = []
    for rel in listed:
        if not _INSTRUCTION_FILE_RE.search(rel):
            continue
        path = repo / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("could not read %s: %s", path, exc)
            continue
        if _GRAPH_MENTION_RE.search(text):
            warnings.append(f"{rel} mentions the graph tool")
    return warnings


def _build_graph(sandbox: ArmSandbox, timeout: int) -> None:
    assert sandbox.data_dir is not None and sandbox.crg_home is not None
    sandbox.data_dir.mkdir(parents=True, exist_ok=True)
    sandbox.crg_home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **sandbox.gryphon_env()}
    started = time.monotonic()
    subprocess.run(
        [sys.executable, "-m", "gryphon", "build", "--repo", str(sandbox.repo)],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=True, timeout=timeout,
    )
    sandbox.graph_build_seconds = round(time.monotonic() - started, 1)


def prepare_arm(
    case: ReviewCase,
    arm: str,
    workdir: Path,
    *,
    fresh: bool = False,
    build_timeout: int = 3600,
) -> ArmSandbox:
    """Create (or reuse) the sanitized clone *arm* runs in.

    Both graph arms get the same clone and graph, so the build is paid once
    per case and they review identical state.
    """
    kind = sandbox_kind(arm)
    root = workdir / _opaque_name(case.id, kind)
    sandbox = ArmSandbox(case_id=case.id, kind=kind, root=root, repo=root / "repo")
    if kind == "graph":
        # Hidden sibling of the clone, never inside it.
        sandbox.data_dir = root / ".data"
        sandbox.crg_home = root / ".home"

    marker = root / _READY_MARKER
    if fresh and root.exists():
        _rmtree(root)
    if not marker.exists():
        if root.exists():
            _rmtree(root)
        _clone(case, sandbox.repo)
        if kind == "graph":
            _build_graph(sandbox, build_timeout)
        marker.write_text(str(sandbox.graph_build_seconds or ""), encoding="utf-8")
    elif kind == "graph":
        recorded = marker.read_text(encoding="utf-8").strip()
        sandbox.graph_build_seconds = float(recorded) if recorded else None

    sandbox.contamination_warnings = contamination_warnings(sandbox.repo)
    return sandbox


def changed_files(sandbox: ArmSandbox) -> list[dict]:
    """``git diff --numstat base...review`` as a list of dicts."""
    out = _git(
        ["diff", "--numstat", f"{BASE_BRANCH}...{REVIEW_BRANCH}"], sandbox.repo,
    ).stdout
    files = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        files.append({
            "path": path,
            "added": int(added) if added.isdigit() else None,
            "deleted": int(deleted) if deleted.isdigit() else None,
        })
    return files

"""Tests for the PostToolUse ``git diff`` graph context (gryphon/diff_context.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gryphon.diff_context import build_diff_context, extract_git_diff_args
from gryphon.graph import GraphStore
from gryphon.parser import EdgeInfo, NodeInfo


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git diff base...review", [["base...review"]]),
        ("git diff --stat base...review -- src/a.py", [["base...review", "--", "src/a.py"]]),
        ("cd . && git diff base...review | head -50", [["base...review"]]),
        # Options never reach git, whatever they are.
        ("git diff --output=/tmp/x --ext-diff base...review", [["base...review"]]),
        ("git -c core.pager=less diff HEAD~1", [["HEAD~1"]]),
        # A redirection stops the argument list.
        ("git diff base...review -- a.py > out.txt", [["base...review", "--", "a.py"]]),
        ("git diff", [[]]),
        ("git diff base...review -- ':!tests/'", [["base...review", "--", ":!tests/"]]),
        ("echo git diff", []),
        ("git log --oneline", []),
        ("git status; git diff main...feature", [["main...feature"]]),
        ("grep 'unbalanced", []),
    ],
)
def test_extract_git_diff_args(command, expected):
    assert extract_git_diff_args(command) == expected


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """``lib.py::total`` changes on ``feature``; ``app.py::report`` calls it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "lib.py").write_text(
        "def total(xs):\n    return sum(xs)\n\n\ndef unused():\n    return 1\n",
        encoding="utf-8",
    )
    (repo / "app.py").write_text("from lib import total\n\n\ndef report(xs):\n"
                                 "    return total(xs)\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "lib.py").write_text(
        "def total(xs):\n    return sum(xs) + 1\n\n\ndef unused():\n    return 2\n",
        encoding="utf-8",
    )
    _git(repo, "commit", "-q", "-am", "change")
    return repo


def _store(tmp_path: Path, repo: Path, *, with_test: bool = False) -> GraphStore:
    root = repo.as_posix()
    store = GraphStore(tmp_path / "graph.db")
    store.upsert_node(NodeInfo(kind="Function", name="total", file_path=f"{root}/lib.py",
                               line_start=1, line_end=2, language="python"))
    store.upsert_node(NodeInfo(kind="Function", name="unused", file_path=f"{root}/lib.py",
                               line_start=5, line_end=6, language="python"))
    store.upsert_node(NodeInfo(kind="Function", name="report", file_path=f"{root}/app.py",
                               line_start=4, line_end=5, language="python"))
    store.upsert_edge(EdgeInfo(kind="CALLS", source=f"{root}/app.py::report",
                               target=f"{root}/lib.py::total", file_path=f"{root}/app.py",
                               line=5))
    if with_test:
        store.upsert_node(NodeInfo(kind="Test", name="test_total",
                                   file_path=f"{root}/tests/test_lib.py", line_start=1,
                                   line_end=2, language="python", is_test=True))
        store.upsert_edge(EdgeInfo(kind="CALLS", source=f"{root}/tests/test_lib.py::test_total",
                                   target=f"{root}/lib.py::total",
                                   file_path=f"{root}/tests/test_lib.py", line=2))
    return store


def test_context_lists_callers_outside_the_diff_and_untested(tmp_path, repo):
    store = _store(tmp_path, repo)
    try:
        text = build_diff_context(str(repo), ["main...feature"], store=store)
    finally:
        store.close()
    assert "2 changed function(s)/class(es) in 1 file(s)" in text
    assert "- total (lib.py:1) <- report (app.py:5)" in text
    assert "No direct test in the graph (may be covered indirectly) for: total, unused" in text


def test_test_callers_count_as_coverage_not_as_outside_callers(tmp_path, repo):
    store = _store(tmp_path, repo, with_test=True)
    try:
        text = build_diff_context(str(repo), ["main...feature"], store=store)
    finally:
        store.close()
    assert "test_total" not in text
    assert "for: unused" in text  # total is covered by test_total


def test_pathspec_limits_the_diff(tmp_path, repo):
    store = _store(tmp_path, repo)
    try:
        assert build_diff_context(str(repo), ["main...feature", "--", "app.py"], store=store) == ""
    finally:
        store.close()


def test_name_only_edges_need_a_unique_name(tmp_path, repo):
    """``update``-style names must not pull in every same-name call site."""
    root = repo.as_posix()
    store = _store(tmp_path, repo)
    try:
        # A second `total` elsewhere makes the bare name ambiguous.
        store.upsert_node(NodeInfo(kind="Function", name="total", file_path=f"{root}/other.py",
                                   line_start=1, line_end=2, language="python"))
        store.upsert_node(NodeInfo(kind="Function", name="caller", file_path=f"{root}/x.py",
                                   line_start=1, line_end=2, language="python"))
        store.upsert_edge(EdgeInfo(kind="CALLS", source=f"{root}/x.py::caller", target="total",
                                   file_path=f"{root}/x.py", line=2))
        text = build_diff_context(str(repo), ["main...feature"], store=store)
    finally:
        store.close()
    assert "report (app.py:5)" in text  # exact edge still found
    assert "caller (x.py" not in text


def test_vendored_files_are_ignored(tmp_path, repo):
    (repo / "staticfiles").mkdir()
    (repo / "staticfiles" / "app.min.js").write_text("function a(){}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "vendored")
    root = repo.as_posix()
    store = _store(tmp_path, repo)
    try:
        store.upsert_node(NodeInfo(kind="Function", name="a",
                                   file_path=f"{root}/staticfiles/app.min.js",
                                   line_start=1, line_end=1, language="javascript"))
        text = build_diff_context(str(repo), ["main...feature"], store=store)
    finally:
        store.close()
    assert "app.min.js" not in text and "in 1 file(s)" in text


def test_bad_revision_yields_nothing(tmp_path, repo):
    store = _store(tmp_path, repo)
    try:
        assert build_diff_context(str(repo), ["no-such-ref"], store=store) == ""
    finally:
        store.close()


def test_cd_before_git_diff_sets_where_pathspecs_resolve():
    from gryphon.diff_context import extract_git_diffs

    assert extract_git_diffs("cd src/app && git diff base...review -- x.py") == [
        ("src/app", ["base...review", "--", "x.py"]),
    ]
    assert extract_git_diffs("git diff a...b") == [("", ["a...b"])]


def test_pathspec_after_cd_is_resolved_from_that_directory(tmp_path, repo):
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pkg")
    (repo / "pkg" / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "change pkg")
    root = repo.as_posix()
    store = _store(tmp_path, repo)
    try:
        store.upsert_node(NodeInfo(kind="Function", name="f", file_path=f"{root}/pkg/mod.py",
                                   line_start=1, line_end=2, language="python"))
        # `cd pkg && git diff HEAD~1 -- mod.py`: the pathspec is relative to pkg/.
        text = build_diff_context(str(repo), ["HEAD~1", "--", "mod.py"],
                                  cwd=str(repo / "pkg"), store=store)
    finally:
        store.close()
    assert "1 changed function(s)/class(es) in 1 file(s)" in text

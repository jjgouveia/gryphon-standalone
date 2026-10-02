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
    assert "- total (lib.py:1) [return] <- report (app.py:5)" in text
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


def test_no_outside_callers_message_names_the_static_limit(tmp_path, repo):
    store = _store(tmp_path, repo)
    try:
        # Only lib.py changes and its one caller (app.py) is dropped from the graph.
        store.remove_file_data(f"{repo.as_posix()}/app.py")
        text = build_diff_context(str(repo), ["main...feature"], store=store)
    finally:
        store.close()
    assert "No callers outside this diff were found statically" in text
    assert "signals, decorators and dynamic dispatch" in text


def _change(repo: Path, path: str, before: str, after: str) -> None:
    """Commit *before* on main and *after* on a fresh ``feature`` branch."""
    _git(repo, "checkout", "-q", "main")
    (repo / path).write_text(before, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"base {path}")
    _git(repo, "branch", "-q", "-D", "feature")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / path).write_text(after, encoding="utf-8")
    _git(repo, "commit", "-q", "-am", f"change {path}")


def _fn(store, root: str, path: str, name: str, start: int, end: int, **kw) -> None:
    store.upsert_node(NodeInfo(kind=kw.pop("kind", "Function"), name=name,
                               file_path=f"{root}/{path}", line_start=start, line_end=end,
                               language="python", **kw))


def _call(store, root: str, src: str, target: str, line: int = 1) -> None:
    store.upsert_edge(EdgeInfo(kind="CALLS", source=f"{root}/{src}", target=target,
                               file_path=f"{root}/{src.split('::')[0]}", line=line))


def test_parse_diff_changes_places_removed_lines_and_skips_deleted_files():
    from gryphon.diff_context import Change, parse_diff_changes

    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -2 +2 @@ def f():\n-    return 1\n+    return 2\n"
        "@@ -9,2 +8,0 @@\n-x = 1\n-y = 2\n"
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-def g(): pass\n"
    )
    assert parse_diff_changes(diff) == {"a.py": [
        Change(2, "-", "    return 1"), Change(2, "+", "    return 2"),
        Change(8, "-", "x = 1"), Change(8, "-", "y = 2"),
    ]}


@pytest.mark.parametrize(
    ("kind", "lines", "inner", "expected"),
    [
        ("Function", [(1, "+", "def total(xs, start=0):")], [], ["signature"]),
        ("Function", [(2, "+", "    return sum(xs) + 1")], [], ["return"]),
        ("Function", [(2, "-", "    raise ValueError(x)")], [], ["raise"]),
        ("Function", [(2, "+", "    n = len(xs)")], [], []),
        ("Function", [(2, "+", "    # return early")], [], []),
        # A nested function's return is its own, not the outer one's.
        ("Function", [(3, "+", "        return 1")], [(2, 3)], []),
        ("Class", [(2, "+", "    status = models.CharField()")], [], ["fields"]),
        ("Class", [(4, "+", "        return 1")], [(3, 4)], []),
        ("Function", [(1, "+", "export const total = (xs: number[]) => {")], [], ["signature"]),
    ],
)
def test_contract_changes(kind, lines, inner, expected):
    from types import SimpleNamespace

    from gryphon.diff_context import Change, contract_changes

    node = SimpleNamespace(kind=kind, name="total", line_start=1, line_end=10)
    changes = [Change(*line) for line in lines]
    assert contract_changes(node, changes, inner) == expected


def test_body_only_change_keeps_its_callers_after_contract_changes(tmp_path, repo):
    """The pilot's hook-only find was a JSX change inside ``return (...)``."""
    _change(repo, "lib.py",
            "def total(xs):\n    n = 1\n    return n\n\n\ndef unused():\n    return 1\n",
            "def total(xs):\n    n = 2\n    return n\n\n\ndef unused():\n    return 2\n")
    root = repo.as_posix()
    store = _store(tmp_path, repo)
    try:
        store.upsert_node(NodeInfo(kind="Function", name="total", file_path=f"{root}/lib.py",
                                   line_start=1, line_end=3, language="python"))
        store.upsert_node(NodeInfo(kind="Function", name="unused", file_path=f"{root}/lib.py",
                                   line_start=6, line_end=7, language="python"))
        _fn(store, root, "z.py", "zeta", 1, 2)
        _call(store, root, "z.py::zeta", f"{root}/lib.py::unused")
        text = build_diff_context(str(repo), ["main...feature"], store=store)
    finally:
        store.close()
    lines = text.splitlines()
    contract = lines.index("- unused (lib.py:6) [return] <- zeta (z.py:1)")
    body = lines.index("- total (lib.py:1) <- report (app.py:5)")
    assert contract < body


def test_callers_are_ranked_by_how_often_they_are_called(tmp_path, repo):
    root = repo.as_posix()
    store = _store(tmp_path, repo)
    try:
        _fn(store, root, "b.py", "busy", 1, 2)
        _call(store, root, "b.py::busy", f"{root}/lib.py::total")
        for i in range(3):
            _fn(store, root, f"u{i}.py", f"user{i}", 1, 2)
            _call(store, root, f"u{i}.py::user{i}", f"{root}/b.py::busy")
        text = build_diff_context(str(repo), ["main...feature"], store=store)
    finally:
        store.close()
    assert "[return] <- busy (b.py:1), report (app.py:5)" in text


def test_name_only_caller_is_labelled(tmp_path, repo):
    root = repo.as_posix()
    store = _store(tmp_path, repo)
    try:
        _fn(store, root, "x.py", "caller", 1, 2)
        _call(store, root, "x.py::caller", "total", line=2)
        text = build_diff_context(str(repo), ["main...feature"], store=store)
    finally:
        store.close()
    assert "caller (by name) (x.py:2)" in text


def test_uncalled_framework_entry_points_say_why(tmp_path, repo):
    _change(repo, "hooks.py",
            "def on_paid(sender, **kw):\n    return 1\n\n\nclass Order:\n"
            "    def save(self):\n        return 1\n",
            "def on_paid(sender, **kw):\n    return 2\n\n\nclass Order:\n"
            "    def save(self):\n        return 2\n")
    root = repo.as_posix()
    store = _store(tmp_path, repo)
    try:
        _fn(store, root, "hooks.py", "on_paid", 1, 2,
            extra={"decorators": ["receiver(post_save)"]})
        _fn(store, root, "hooks.py", "Order", 5, 7, kind="Class")
        _fn(store, root, "hooks.py", "save", 6, 7, parent_name="Order")
        store.upsert_edge(EdgeInfo(kind="INHERITS", source=f"{root}/hooks.py::Order",
                                   target="models.Model", file_path=f"{root}/hooks.py",
                                   line=5))
        text = build_diff_context(str(repo), ["main...feature"], store=store)
    finally:
        store.close()
    assert ("No static caller, likely called by a framework or a base class: "
            "on_paid (decorated @receiver), save (method of Order(models.Model))") in text


def test_long_context_is_cut_within_budget_and_says_so(tmp_path, repo):
    from gryphon.diff_context import FOOTER, MAX_CHARS

    body = "".join(f"def f{i}(x):\n    return x\n\n\n" for i in range(60))
    _change(repo, "many.py", body, body.replace("return x", "return x + 1"))
    root = repo.as_posix()
    store = _store(tmp_path, repo)
    try:
        for i in range(60):
            _fn(store, root, "many.py", f"function_with_a_long_name_{i}", 4 * i + 1, 4 * i + 2)
            for j in range(6):
                _fn(store, root, f"c{i}_{j}.py", f"caller_with_a_long_name_{i}_{j}", 1, 2)
                _call(store, root, f"c{i}_{j}.py::caller_with_a_long_name_{i}_{j}",
                      f"{root}/many.py::function_with_a_long_name_{i}")
        text = build_diff_context(str(repo), ["main...feature"], store=store)
    finally:
        store.close()
    assert len(text) <= MAX_CHARS
    assert text.endswith(FOOTER)
    assert "(also with outside callers: function_with_a_long_name_" in text


def test_header_warns_when_the_graph_is_older_than_head(tmp_path, repo):
    store = _store(tmp_path, repo)
    try:
        store.set_metadata("git_head_sha", "0" * 40)
        text = build_diff_context(str(repo), ["main...feature"], store=store)
    finally:
        store.close()
    assert "(graph built at 00000000, HEAD is " in text
    assert "line numbers may be off)" in text


def test_lines_past_the_budget_are_counted_not_cut_mid_line(tmp_path, repo, monkeypatch):
    import gryphon.diff_context as dc

    monkeypatch.setattr(dc, "MAX_CHARS", len(dc.FOOTER) + 200)
    store = _store(tmp_path, repo)
    try:
        text = build_diff_context(str(repo), ["main...feature"], store=store)
    finally:
        store.close()
    lines = text.splitlines()
    assert lines[0].startswith("[gryphon] Graph context")
    assert lines[-2].startswith("(") and lines[-2].endswith("more line(s) cut to keep this short.)")
    assert lines[-1] == dc.FOOTER

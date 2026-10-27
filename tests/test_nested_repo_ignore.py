"""Nested worktrees and clones stay out of the parent graph.

A review worktree created inside the repository used to be indexed by the
watcher: a Django graph grew from ~50k to ~350k nodes and the MCP server spent
minutes rebuilding it.  Linked worktrees (``.git`` file pointing at
``.git/worktrees``) and nested clones (``.git`` directory) must be skipped;
submodules (``.git`` file pointing at ``.git/modules``) keep the existing
``CRG_RECURSE_SUBMODULES`` opt-in.
"""

from pathlib import Path

from watchdog.events import DirCreatedEvent, FileCreatedEvent

from gryphon.graph import GraphStore
from gryphon.incremental import (
    _create_watch_handler,
    _is_nested_repo_path,
    collect_all_files,
    incremental_update,
)
from gryphon.parser import CodeParser


def write_python(path: Path, name: str = "module") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"def {name}():\n    return 1\n", encoding="utf-8")
    return path


def make_linked_worktree(directory: Path, name: str = "wt") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".git").write_text(
        f"gitdir: /repo/.git/worktrees/{name}\n",
        encoding="utf-8",
    )


def make_nested_clone(directory: Path) -> None:
    (directory / ".git").mkdir(parents=True, exist_ok=True)


def make_submodule(directory: Path, name: str = "sub") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".git").write_text(
        f"gitdir: ../.git/modules/{name}\n",
        encoding="utf-8",
    )


def test_linked_worktree_path_is_nested(tmp_path):
    worktree = tmp_path / ".wt-pr1"
    make_linked_worktree(worktree, "wt-pr1")
    write_python(worktree / "module.py")

    assert _is_nested_repo_path(tmp_path, ".wt-pr1/module.py") is True
    assert _is_nested_repo_path(tmp_path, "module.py") is False


def test_linked_worktree_relative_gitdir_is_nested(tmp_path):
    worktree = tmp_path / "libs" / "wt"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(
        "gitdir: ../../.git/worktrees/wt\n",
        encoding="utf-8",
    )
    write_python(worktree / "module.py")

    assert _is_nested_repo_path(tmp_path, "libs/wt/module.py") is True


def test_nested_clone_path_is_nested(tmp_path):
    clone = tmp_path / "vendor" / "lib"
    make_nested_clone(clone)
    write_python(clone / "module.py")

    assert _is_nested_repo_path(tmp_path, "vendor/lib/module.py") is True


def test_submodule_path_is_not_nested(tmp_path):
    submodule = tmp_path / "vendor" / "sub"
    make_submodule(submodule)
    write_python(submodule / "module.py")

    assert _is_nested_repo_path(tmp_path, "vendor/sub/module.py") is False


def test_collect_all_files_skips_linked_worktree(tmp_path, monkeypatch):
    write_python(tmp_path / "app.py", "app")
    worktree = tmp_path / ".wt-pr1"
    make_linked_worktree(worktree, "wt-pr1")
    write_python(worktree / "module.py")
    monkeypatch.setattr(
        "gryphon.incremental.get_all_tracked_files",
        lambda *args, **kwargs: ["app.py", ".wt-pr1/module.py"],
    )

    assert collect_all_files(tmp_path) == ["app.py"]


def test_collect_all_files_keeps_submodule(tmp_path, monkeypatch):
    write_python(tmp_path / "app.py", "app")
    submodule = tmp_path / "libs" / "sub"
    make_submodule(submodule)
    write_python(submodule / "module.py")
    monkeypatch.setattr(
        "gryphon.incremental.get_all_tracked_files",
        lambda *args, **kwargs: ["app.py", "libs/sub/module.py"],
    )

    assert collect_all_files(tmp_path) == ["app.py", "libs/sub/module.py"]


def test_watch_handler_skips_linked_worktree_file(tmp_path):
    with GraphStore(tmp_path / "graph.db") as store:
        worktree = tmp_path / ".wt-pr1"
        make_linked_worktree(worktree, "wt-pr1")
        module = write_python(worktree / "module.py")
        handler = _create_watch_handler(tmp_path, store, None)

        handler.process([FileCreatedEvent(str(module))])
        handler.raise_if_failed()

        assert store.get_all_files() == []


def test_watch_handler_skips_created_worktree_directory(tmp_path):
    with GraphStore(tmp_path / "graph.db") as store:
        worktree = tmp_path / ".wt-pr1"
        make_linked_worktree(worktree, "wt-pr1")
        write_python(worktree / "module.py")
        handler = _create_watch_handler(tmp_path, store, None)

        handler.process([DirCreatedEvent(str(worktree))])
        handler.raise_if_failed()

        assert store.get_all_files() == []


def test_incremental_update_skips_linked_worktree(tmp_path):
    with GraphStore(tmp_path / "graph.db") as store:
        worktree = tmp_path / ".wt-pr1"
        make_linked_worktree(worktree, "wt-pr1")
        module = write_python(worktree / "module.py")

        result = incremental_update(
            tmp_path,
            store,
            changed_files=[str(module)],
            reconcile_stale=False,
        )

        assert result["files_updated"] == 0
        assert store.get_all_files() == []


def test_full_update_purges_previously_indexed_worktree(tmp_path):
    with GraphStore(tmp_path / "graph.db") as store:
        worktree = tmp_path / ".wt-pr1"
        make_linked_worktree(worktree, "wt-pr1")
        module = write_python(worktree / "module.py")
        parser = CodeParser(tmp_path)
        nodes, edges = parser.parse_bytes(module, module.read_bytes())
        store.store_file_nodes_edges(str(module), nodes, edges, "0" * 64)
        assert store.get_nodes_by_file(str(module))

        result = incremental_update(
            tmp_path,
            store,
            changed_files=[],
            reconcile_stale=True,
        )

        assert result["stale_files_removed"] >= 1
        assert store.get_nodes_by_file(str(module)) == []

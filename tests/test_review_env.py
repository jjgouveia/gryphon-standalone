"""Contract tests for ``scripts/review_env.py``.

The dangerous parts of dependency reuse are the filesystem operations: a
junction that is mistaken for a real directory can make ``rmtree`` delete the
main checkout, and an environment keyed by the wrong lockfile runs a review
against package versions the PR does not use. These tests pin both.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import review_env  # noqa: E402


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def write_node_project(repo: Path, *, workspaces: bool = False) -> None:
    manifest = {"name": "demo", "version": "1.0.0", "private": True}
    if workspaces:
        manifest["workspaces"] = ["packages/*"]
    (repo / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    lock = {
        "name": "demo",
        "version": "1.0.0",
        "lockfileVersion": 3,
        "packages": {"": {"name": "demo", "version": "1.0.0"}},
    }
    (repo / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")


@pytest.fixture
def repo_pair(tmp_path: Path) -> tuple[Path, Path]:
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q")
    git(main, "config", "user.email", "test@example.com")
    git(main, "config", "user.name", "Test")
    write_node_project(main)
    git(main, "add", "package.json", "package-lock.json")
    git(main, "commit", "-qm", "init")
    worktree = tmp_path / "worktree"
    git(main, "worktree", "add", "-q", "-b", "review", str(worktree))
    return main, worktree


def remove_worktree_link(worktree: Path) -> None:
    """Remove only the link, never the tree it points at.

    ``shutil.rmtree`` treats a Windows junction as a directory and would delete
    the main checkout's ``node_modules``. Tests clean up through this helper so
    a failure cannot escape the test's temporary directory.
    """
    path = worktree / "node_modules"
    if review_env._is_dir_link(path):
        review_env._remove_dir_link(path)


def test_plan_links_main_node_modules_without_installing(
    repo_pair: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main, worktree = repo_pair
    sentinel = main / "node_modules" / "sentinel.txt"
    sentinel.parent.mkdir()
    sentinel.write_text("main tree", encoding="utf-8")
    cache = tmp_path / "cache"
    try:
        assert review_env.main(["plan", "--repo", str(worktree), "--cache-dir", str(cache)]) == 0
        assert "will-link-main" in capsys.readouterr().out

        assert review_env.main(["prepare", "--repo", str(worktree), "--cache-dir", str(cache)]) == 0
        capsys.readouterr()
        assert review_env._is_dir_link(worktree / "node_modules")
        linked = worktree / "node_modules" / "sentinel.txt"
        assert linked.read_text(encoding="utf-8") == "main tree"
        assert (
            review_env._link_target(worktree / "node_modules")
            == main.joinpath("node_modules").resolve()
        )

        code = review_env.main(
            ["plan", "--repo", str(worktree), "--cache-dir", str(cache), "--json"]
        )
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["node"]["action"] == "reused-link"
        assert payload["node"]["key"] == payload["node"]["base_key"]
    finally:
        remove_worktree_link(worktree)
    assert sentinel.read_text(encoding="utf-8") == "main tree"


def test_prepare_refuses_real_node_modules_without_force(
    repo_pair: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main, worktree = repo_pair
    (main / "node_modules").mkdir()
    stale = worktree / "node_modules"
    stale.mkdir()
    (stale / "stale.txt").write_text("old", encoding="utf-8")
    cache = tmp_path / "cache"
    try:
        code = review_env.main(["prepare", "--repo", str(worktree), "--cache-dir", str(cache)])
        assert code == 3
        assert (stale / "stale.txt").read_text(encoding="utf-8") == "old"
        assert not review_env._is_dir_link(stale)

        code = review_env.main(
            ["prepare", "--repo", str(worktree), "--cache-dir", str(cache), "--force"]
        )
        assert code == 0
        assert review_env._is_dir_link(stale)
        backups = list(worktree.glob("node_modules.gryphon-old*"))
        assert len(backups) == 1
        assert (backups[0] / "stale.txt").read_text(encoding="utf-8") == "old"
    finally:
        remove_worktree_link(worktree)


def test_remove_link_does_not_delete_the_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    link = tmp_path / "link"
    review_env._make_dir_link(link, target)
    assert review_env._is_dir_link(link)
    assert review_env._link_target(link) == target.resolve()
    review_env._remove_dir_link(link)
    assert not link.exists()
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    ("lockfile", "expected"),
    [
        ("package-lock.json", "npm"),
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lock", "bun"),
    ],
)
def test_node_manager_detection(tmp_path: Path, lockfile: str, expected: str) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / lockfile).write_text("", encoding="utf-8")
    assert review_env.node_manager(tmp_path) == expected


def test_dependency_key_changes_with_lockfile_and_runtime(tmp_path: Path) -> None:
    write_node_project(tmp_path)
    files = review_env.node_files(tmp_path)
    first = review_env.dependency_key(
        tmp_path, files, ecosystem="node", manager="npm", runtime="v20.0.0"
    )
    second = review_env.dependency_key(
        tmp_path, files, ecosystem="node", manager="npm", runtime="v20.0.0"
    )
    assert first == second

    (tmp_path / "package-lock.json").write_text(
        json.dumps({"lockfileVersion": 3, "packages": {}}), encoding="utf-8"
    )
    changed = review_env.dependency_key(
        tmp_path,
        review_env.node_files(tmp_path),
        ecosystem="node",
        manager="npm",
        runtime="v20.0.0",
    )
    assert changed != first

    runtime_changed = review_env.dependency_key(
        tmp_path,
        review_env.node_files(tmp_path),
        ecosystem="node",
        manager="npm",
        runtime="v22.0.0",
    )
    assert runtime_changed != changed


def test_python_key_reuses_the_main_environment_only_when_the_lock_matches(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    main.mkdir()
    worktree.mkdir()
    for repo in (main, worktree):
        (repo / "pyproject.toml").write_text('[tool.poetry]\nname = "demo"\n', encoding="utf-8")
        (repo / "poetry.lock").write_text("lock-v1", encoding="utf-8")
        (repo / ".python-version").write_text("3.12\n", encoding="utf-8")
    venv = main / ".venv"
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    python.write_text("", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text("version = 3.12.10\n", encoding="utf-8")

    plan = review_env.plan_python(worktree, main, None, tmp_path / "cache", fast=False)
    assert plan is not None
    assert plan["action"] == "reused-base"
    assert plan["interpreter"] == python
    assert plan["key"] == plan["base_key"]

    (worktree / "poetry.lock").write_text("lock-v2", encoding="utf-8")
    changed = review_env.plan_python(worktree, main, None, tmp_path / "cache", fast=False)
    assert changed is not None
    assert changed["action"] == "will-create"
    assert changed["base_key"] != changed["key"]

    fast = review_env.plan_python(worktree, main, None, tmp_path / "cache", fast=True)
    assert fast is not None
    assert fast["action"] == "will-sync-base"
    assert fast["env"] == venv


def test_base_environment_with_the_project_installed_is_not_shared(tmp_path: Path) -> None:
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    main.mkdir()
    worktree.mkdir()
    for repo in (main, worktree):
        (repo / "pyproject.toml").write_text('[tool.poetry]\nname = "demo"\n', encoding="utf-8")
        (repo / "poetry.lock").write_text("lock-v1", encoding="utf-8")
        (repo / ".python-version").write_text("3.12\n", encoding="utf-8")
    venv = main / ".venv"
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    (scripts / ("python.exe" if os.name == "nt" else "python")).write_text("", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text("version = 3.12.10\n", encoding="utf-8")
    if os.name == "nt":
        site = venv / "Lib" / "site-packages"
    else:
        site = venv / "lib" / "python3.12" / "site-packages"
    metadata = site / "demo-1.0.0.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Metadata-Version: 2.1\nName: demo\nVersion: 1.0.0\n", encoding="utf-8")

    plan = review_env.plan_python(worktree, main, None, tmp_path / "cache", fast=False)
    assert plan is not None
    assert plan["action"] == "will-create"
    assert plan["warning"] is not None
    assert "installed from the main checkout" in plan["warning"]

    (worktree / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "demo"\npackage-mode = false\n', encoding="utf-8"
    )
    (main / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "demo"\npackage-mode = false\n', encoding="utf-8"
    )
    uninstalled = review_env.plan_python(worktree, main, None, tmp_path / "cache", fast=False)
    assert uninstalled is not None
    assert uninstalled["action"] == "reused-base"


def test_an_unfinished_cache_environment_is_not_reused(tmp_path: Path) -> None:
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    main.mkdir()
    worktree.mkdir()
    for repo in (main, worktree):
        (repo / "pyproject.toml").write_text(
            '[tool.poetry]\nname = "demo"\npackage-mode = false\n', encoding="utf-8"
        )
        (repo / "poetry.lock").write_text("lock-v1", encoding="utf-8")
        (repo / ".python-version").write_text("3.12\n", encoding="utf-8")
    (worktree / "poetry.lock").write_text("lock-v2", encoding="utf-8")
    files = review_env.python_files(worktree)
    key = review_env.dependency_key(
        worktree, files, ecosystem="python", manager="poetry", runtime="py3.12"
    )
    cache = tmp_path / "cache"
    env = review_env._python_cache_dir(cache, main, key, "3.12")
    scripts = env / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    (scripts / ("python.exe" if os.name == "nt" else "python")).write_text("", encoding="utf-8")
    (env / ".gryphon-review-env.json").write_text(
        json.dumps({"v": 1, "key": key, "state": "installing"}), encoding="utf-8"
    )

    plan = review_env.plan_python(worktree, main, None, cache, fast=False)
    assert plan is not None
    assert plan["action"] == "will-create"


def test_poetry_project_reads_pep_621_name_and_package_mode(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo-app"\n\n[tool.poetry]\npackage-mode = false\n',
        encoding="utf-8",
    )
    assert review_env._poetry_project(tmp_path) == ("demo-app", False)


def test_fast_is_refused_when_the_base_environment_is_a_link(tmp_path: Path) -> None:
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    main.mkdir()
    worktree.mkdir()
    for repo in (main, worktree):
        (repo / "pyproject.toml").write_text(
            '[tool.poetry]\nname = "demo"\npackage-mode = false\n', encoding="utf-8"
        )
        (repo / "poetry.lock").write_text("lock-v1", encoding="utf-8")
        (repo / ".python-version").write_text("3.12\n", encoding="utf-8")
    real = tmp_path / "real-env"
    scripts = real / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    (scripts / ("python.exe" if os.name == "nt" else "python")).write_text("", encoding="utf-8")
    (real / "pyvenv.cfg").write_text("version = 3.12.10\n", encoding="utf-8")
    review_env._make_dir_link(main / ".venv", real)
    try:
        (worktree / "poetry.lock").write_text("lock-v2", encoding="utf-8")
        plan = review_env.plan_python(worktree, main, None, tmp_path / "cache", fast=True)
        assert plan is not None
        assert plan["action"] == "will-create"
        assert plan["warning"] is not None
        assert "is a link" in plan["warning"]
    finally:
        review_env._remove_dir_link(main / ".venv")


def test_fast_is_refused_when_the_base_interpreter_differs(tmp_path: Path) -> None:
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    main.mkdir()
    worktree.mkdir()
    for repo in (main, worktree):
        (repo / "pyproject.toml").write_text('[tool.poetry]\nname = "demo"\n', encoding="utf-8")
        (repo / "poetry.lock").write_text("lock-v1", encoding="utf-8")
        (repo / ".python-version").write_text("3.12\n", encoding="utf-8")
    venv = main / ".venv"
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    (scripts / ("python.exe" if os.name == "nt" else "python")).write_text("", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text("version = 3.14.1\n", encoding="utf-8")

    (worktree / "poetry.lock").write_text("lock-v2", encoding="utf-8")
    plan = review_env.plan_python(worktree, main, None, tmp_path / "cache", fast=True)
    assert plan is not None
    assert plan["action"] == "will-create"
    assert plan["warning"] is not None
    assert "3.14" in plan["warning"]
    assert "3.12" in plan["warning"]


def test_npm_tree_current_ignores_pruned_optional_platform_package(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    current_os = "win32" if sys.platform == "win32" else "linux"
    other_os = "linux" if current_os == "win32" else "win32"
    lock = {
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "demo", "version": "1.0.0"},
            "node_modules/required": {"version": "1.0.0", "integrity": "sha512-a"},
            "node_modules/optional-native": {
                "version": "1.0.0",
                "optional": True,
                "os": [other_os],
            },
            "node_modules/optional-native/node_modules/helper": {
                "version": "1.0.0",
            },
        },
    }
    (tmp_path / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    hidden = {
        "packages": {
            "node_modules/required": {"version": "1.0.0", "integrity": "sha512-a"},
        }
    }
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / ".package-lock.json").write_text(
        json.dumps(hidden), encoding="utf-8"
    )
    assert review_env._npm_tree_current(tmp_path) is True

    lock["packages"]["node_modules/required-two"] = {
        "version": "1.0.0",
        "integrity": "sha512-b",
    }
    (tmp_path / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    assert review_env._npm_tree_current(tmp_path) is False


def test_force_replaces_real_node_modules_when_the_key_changed(
    repo_pair: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main, worktree = repo_pair
    (main / "node_modules").mkdir()
    lock_path = worktree / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["packages"]["node_modules/new"] = {"version": "1.0.0"}
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    stale = worktree / "node_modules"
    stale.mkdir()
    (stale / "stale.txt").write_text("old", encoding="utf-8")
    cache = tmp_path / "cache"

    def fake_install(source: Path, cache_dir: Path, manager: str, key: str) -> None:
        modules = cache_dir / "node_modules"
        modules.mkdir(parents=True)
        (modules / "fresh.txt").write_text("fresh", encoding="utf-8")
        review_env._write_marker(cache_dir, key, ecosystem="node", manager=manager)

    monkeypatch.setattr(review_env, "_install_node_cache", fake_install)
    try:
        assert review_env.main(["prepare", "--repo", str(worktree), "--cache-dir", str(cache)]) == 3
        assert (stale / "stale.txt").exists()

        assert (
            review_env.main(
                ["prepare", "--repo", str(worktree), "--cache-dir", str(cache), "--force"]
            )
            == 0
        )
        assert review_env._is_dir_link(worktree / "node_modules")
        assert (worktree / "node_modules" / "fresh.txt").read_text(encoding="utf-8") == "fresh"
        backups = list(worktree.glob("node_modules.gryphon-old*"))
        assert len(backups) == 1
        assert (backups[0] / "stale.txt").read_text(encoding="utf-8") == "old"
    finally:
        remove_worktree_link(worktree)


def test_non_git_copy_with_a_matching_link_is_kept(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    copy = tmp_path / "copy"
    origin.mkdir()
    copy.mkdir()
    write_node_project(origin)
    write_node_project(copy)
    (origin / "node_modules").mkdir()
    (origin / "node_modules" / "sentinel.txt").write_text("origin", encoding="utf-8")
    review_env._make_dir_link(copy / "node_modules", origin / "node_modules")
    try:
        assert review_env.resolve_repo(copy) == copy
        plan = review_env.plan_node(copy, copy, None, tmp_path / "cache", force=False)
        assert plan is not None
        assert plan["action"] == "kept-existing"
        assert plan["link_target"] == (origin / "node_modules").resolve()

        lock_path = copy / "package-lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["packages"]["node_modules/new"] = {"version": "1.0.0"}
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        changed = review_env.plan_node(copy, copy, None, tmp_path / "cache", force=False)
        assert changed is not None
        assert changed["action"] == "will-install-cache"
    finally:
        remove_worktree_link(copy)


def test_monorepo_is_refused(repo_pair: tuple[Path, Path], tmp_path: Path) -> None:
    main, worktree = repo_pair
    write_node_project(worktree, workspaces=True)
    (main / "node_modules").mkdir()
    plan = review_env.plan_node(worktree, main, None, tmp_path / "cache", force=False)
    assert plan is not None
    assert plan["action"] == "monorepo"
    assert not (worktree / "node_modules").exists()


def test_cache_inside_repository_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(review_env.ReviewEnvError):
        review_env._resolve_cache(str(repo / ".cache"), repo, repo)


def test_review_skill_documents_dependency_reuse() -> None:
    text = (ROOT / "skills" / "review-pr" / "SKILL.md").read_text(encoding="utf-8")
    assert "scripts/review_env.py" in text
    assert "never run" in text.lower()
    assert "lock hash" in text.lower() or "dependency key" in text.lower()
    assert "Tell the user what happened" in text
    assert "reused-cache" in text
    assert "installing" in text
    multi = (ROOT / "skills" / "review-multi" / "SKILL.md").read_text(encoding="utf-8")
    assert "tell the user" in multi.lower()

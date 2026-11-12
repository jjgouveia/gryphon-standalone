#!/usr/bin/env python3
"""Reuse dependency environments across review worktrees.

A review worktree is a second checkout of the same repository, so its
``node_modules`` and virtualenv are empty even when the dependency inputs are
byte-for-byte identical to the main checkout. Installing them per worktree is
the expensive part of running a review, and it is also a correctness problem:
an environment built for a different lockfile runs the tests against package
versions the PR does not use, which hides the break the review exists to catch.

This script keys an environment by the hash of its dependency inputs:

    ecosystem + manager + runtime + OS/arch
    + package.json/lockfile/.npmrc/patches        (node)
    + pyproject.toml/poetry.lock/.python-version  (python)

and then:

* **node, key matches the main checkout** -> link ``node_modules`` to the main
  checkout. Nothing is installed.
* **node, key differs** -> build one cached ``node_modules`` for that key and
  link every worktree at that state to it. npm has no content-addressed store,
  so the first build is a full ``npm ci``; the second worktree at the same
  lock pays nothing.
* **python, key matches the main checkout** -> reuse the Poetry interpreter.
  Nothing is installed. If the parent environment has the root project itself
  installed, a cache environment is built with ``--no-root`` instead: sharing
  it would make the worktree import the parent's source.
* **python, key differs** -> build one cached virtualenv for that key and reuse
  it from every worktree at that state.
* **python, ``--fast``** -> let ``poetry install`` sync only the delta into the
  main checkout's virtualenv. It is faster, but it mutates that environment;
  ``restore`` puts it back. Never the default.

Safety rules this file enforces:

* a real ``node_modules`` is never deleted: ``--force`` renames it aside and
  links the cached tree;
* an install never runs through a link, because npm/pnpm would write into the
  target and mutate the main checkout;
* a cache directory this script does not own (no marker) is never removed;
* the cache must live outside the repository, so the graph watcher and
  ``git status`` do not see it;
* a missing cache is built once per key, under a lock, so two worktrees cannot
  race the same install.

Exit codes::

    0  environment ready (or a plan was rendered)
    2  nothing to prepare, or an unsupported layout (workspace monorepo)
    3  unsafe state; re-run ``prepare`` with ``--force``
    4  an install or environment creation failed
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    tomllib = None  # type: ignore[assignment]

PROBE_TIMEOUT = 20
INSTALL_TIMEOUT = 3600
CACHE_ENV = "GRYPHON_REVIEW_ENV_CACHE"
MARKER = ".gryphon-review-env.json"
MARKER_VERSION = 1
NODE_LOCKFILES = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb")
NODE_EXTRA_FILES = (".npmrc", ".yarnrc.yml", "bunfig.toml", "pnpm-workspace.yaml")
NODE_INPUT_DIRS = ("patches",)
NODE_COPY_DIRS = ("patches", "scripts", "prisma")
PYTHON_FILES = ("pyproject.toml", "poetry.lock", ".python-version", "poetry.toml")
_TEXT = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}


class ReviewEnvError(RuntimeError):
    """The environment could not be prepared."""


class UnsafeStateError(ReviewEnvError):
    """Preparing would overwrite a real directory the user owns."""


# --- process helpers -------------------------------------------------------


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = PROBE_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            timeout=timeout,
            check=False,
            **_TEXT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReviewEnvError(f"{command[0]}: {exc}") from exc


def _which(name: str) -> str | None:
    return shutil.which(name)


def _git(repo: Path, args: list[str]) -> str:
    proc = _run(["git", "-C", str(repo), *args])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        message = detail[0] if detail else f"git {' '.join(args)} exited {proc.returncode}"
        raise ReviewEnvError(message)
    return proc.stdout


def resolve_repo(path: Path) -> Path:
    """Return the top-level checkout for *path*.

    A linked worktree resolves through git. A plain copy is accepted too: the
    review helper should be usable on a directory snapshot, even though it
    cannot discover a parent checkout from it.
    """
    candidate = path.expanduser().resolve()
    if not candidate.is_dir():
        raise ReviewEnvError(f"{candidate} is not a directory")
    try:
        top = _git(candidate, ["rev-parse", "--show-toplevel"]).strip()
    except ReviewEnvError:
        return candidate
    if not top:
        return candidate
    return Path(top).resolve()


def find_main_checkout(repo: Path) -> Path:
    """Return the checkout that owns the repository's objects.

    A linked worktree's common git directory is ``<main>/.git``; the main
    checkout is its parent. For a bare repository there is no main checkout, so
    the current path is returned and the caller treats its environment as the
    only one available.
    """
    repo = repo.resolve()
    try:
        common = Path(_git(repo, ["rev-parse", "--git-common-dir"]).strip())
    except ReviewEnvError:
        return repo
    if not common.is_absolute():
        common = (repo / common).resolve()
    if common.name == ".git":
        if common.parent == repo:
            return repo
        candidate = common.parent.resolve()
        # A submodule's common dir lives under .git/modules, whose parent is
        # not a checkout. Only accept a candidate that looks like one.
        if (candidate / ".git").is_dir() or any(
            (candidate / name).exists() for name in ("package.json", "pyproject.toml")
        ):
            return candidate
    return repo


# --- hashing ---------------------------------------------------------------


def _hash_tree(digest: Any, path: Path, repo: Path) -> None:
    if path.is_dir():
        for root, dirs, names in os.walk(path, followlinks=False):
            dirs.sort()
            names.sort()
            for name in names:
                child = Path(root) / name
                relative = child.relative_to(repo).as_posix()
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                try:
                    digest.update(child.read_bytes())
                except OSError as exc:
                    raise ReviewEnvError(f"cannot read {child}: {exc}") from exc
    else:
        relative = path.relative_to(repo).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            raise ReviewEnvError(f"cannot read {path}: {exc}") from exc


def dependency_key(
    repo: Path,
    files: tuple[str, ...],
    *,
    ecosystem: str,
    manager: str,
    runtime: str,
) -> str:
    """Hash the inputs that decide what the environment contains.

    Runtime and OS/arch are part of the key because a Python 3.12 virtualenv
    cannot be reused by 3.14 and a Windows ``node_modules`` cannot be reused by
    Linux. The key is the *deciding* fact: a review must not reuse an
    environment whose key differs from the worktree's.
    """
    digest = hashlib.sha256()
    header = "\0".join(
        (
            ecosystem,
            manager,
            runtime,
            platform.system().lower(),
            platform.machine().lower(),
        )
    )
    digest.update(header.encode("utf-8"))
    digest.update(b"\0")
    for relative in sorted(files):
        _hash_tree(digest, repo / relative, repo)
    return digest.hexdigest()


def _node_version() -> str:
    executable = _which("node")
    if not executable:
        return "unknown"
    proc = _run([executable, "--version"])
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def _short(key: str) -> str:
    return key[:12]


# --- node ------------------------------------------------------------------


def node_files(repo: Path) -> tuple[str, ...]:
    if not (repo / "package.json").is_file():
        return ()
    files = ["package.json"]
    files += [name for name in NODE_LOCKFILES if (repo / name).is_file()]
    files += [name for name in NODE_EXTRA_FILES if (repo / name).is_file()]
    files += [name for name in NODE_INPUT_DIRS if (repo / name).is_dir()]
    return tuple(dict.fromkeys(files))


def node_manager(repo: Path) -> str | None:
    if (repo / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (repo / "yarn.lock").is_file():
        return "yarn"
    if (repo / "bun.lock").is_file() or (repo / "bun.lockb").is_file():
        return "bun"
    if (repo / "package-lock.json").is_file() or (repo / "package.json").is_file():
        return "npm"
    return None


def _is_monorepo(repo: Path) -> bool:
    if (repo / "pnpm-workspace.yaml").is_file():
        return True
    manifest = repo / "package.json"
    if not manifest.is_file():
        return False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    workspaces = payload.get("workspaces") if isinstance(payload, dict) else None
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages")
    return bool(workspaces)


def _is_dir_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse)


def _link_target(path: Path) -> Path | None:
    try:
        raw = os.readlink(path)
    except OSError:
        return None
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    target = Path(raw)
    if not target.is_absolute():
        target = path.parent / target
    try:
        return target.resolve()
    except OSError:
        return target


def _describe_node_modules(path: Path) -> dict[str, Any]:
    if _is_dir_link(path):
        return {"kind": "link", "target": _link_target(path)}
    if path.is_dir():
        return {"kind": "dir"}
    if path.exists():
        return {"kind": "other"}
    return {"kind": "missing"}


def _make_dir_link(link: Path, target: Path) -> None:
    if not target.is_dir():
        raise ReviewEnvError(f"link target does not exist: {target}")
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        proc = _run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/c", "mklink", "/J", str(link), str(target)]
        )
        if proc.returncode != 0:
            try:
                os.symlink(target, link, target_is_directory=True)
            except OSError as exc:
                detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                reason = detail[0] if detail else str(exc)
                raise ReviewEnvError(f"cannot create a junction at {link}: {reason}") from exc
    else:
        os.symlink(target, link, target_is_directory=True)
    if not link.is_dir():
        raise ReviewEnvError(f"link was created but does not resolve: {link}")


def _remove_dir_link(path: Path) -> None:
    if stat.S_ISLNK(path.lstat().st_mode):
        path.unlink()
    elif _is_dir_link(path):
        os.rmdir(path)
    else:
        raise ReviewEnvError(f"refusing to remove a real directory: {path}")


def _backup_real_dir(path: Path) -> Path:
    backup = path.with_name(f"{path.name}.gryphon-old")
    if backup.exists() or _is_dir_link(backup):
        backup = path.with_name(f"{path.name}.gryphon-old-{int(time.time())}")
    path.rename(backup)
    return backup


def _platform_tags() -> tuple[str, str]:
    system = platform.system().lower()
    os_name = {"windows": "win32", "darwin": "darwin", "linux": "linux"}.get(system, system)
    machine = platform.machine().lower()
    arch = {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86": "ia32",
        "i386": "ia32",
        "i686": "ia32",
    }.get(machine, machine)
    return arch, os_name


def _platform_matches(values: Any, current: str) -> bool:
    if not isinstance(values, list) or not values:
        return True
    positive = [str(value) for value in values if not str(value).startswith("!")]
    negative = [str(value)[1:] for value in values if str(value).startswith("!")]
    if current in negative:
        return False
    return not positive or current in positive


def _optional_ancestor_skipped(
    name: str,
    expected: dict[str, Any],
    installed: dict[str, Any],
    arch: str,
    os_name: str,
) -> bool:
    """True when *name* lives inside an optional package that was not installed.

    npm prunes an optional platform-specific package together with everything
    nested under it, but the lockfile still lists those nested packages without
    an ``optional`` flag of their own. Walking the ``node_modules/`` prefixes
    keeps them out of the comparison instead of flagging the tree as stale.
    """
    segments = [segment for segment in name.split("node_modules/") if segment]
    prefix = ""
    for segment in segments[:-1]:
        prefix = f"{prefix}node_modules/{segment}".rstrip("/")
        ancestor = expected.get(prefix)
        if not isinstance(ancestor, dict):
            continue
        if ancestor.get("optional") and (
            not _platform_matches(ancestor.get("os"), os_name)
            or not _platform_matches(ancestor.get("cpu"), arch)
            or prefix not in installed
        ):
            return True
    return False


def _npm_tree_current(repo: Path) -> bool | None:
    """Check whether an in-worktree ``node_modules`` matches ``package-lock``.

    npm writes ``node_modules/.package-lock.json`` after a successful install.
    Comparing versions against the lockfile is the one cheap proof that a real
    tree was not left over from a previous lock state. ``None`` means the tree
    could not be verified (not npm, or either file unreadable), which the caller
    reports as a warning instead of claiming it is current.
    """
    lock_path = repo / "package-lock.json"
    hidden_path = repo / "node_modules" / ".package-lock.json"
    if not lock_path.is_file() or not hidden_path.is_file():
        return None
    try:
        expected = json.loads(lock_path.read_text(encoding="utf-8"))
        installed = json.loads(hidden_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    expected_packages = expected.get("packages") if isinstance(expected, dict) else None
    installed_packages = installed.get("packages") if isinstance(installed, dict) else None
    if not isinstance(expected_packages, dict) or not isinstance(installed_packages, dict):
        return None
    expected_packages = dict(expected_packages)
    installed_packages = dict(installed_packages)
    expected_packages.pop("", None)
    installed_packages.pop("", None)
    arch, os_name = _platform_tags()

    for name, entry in expected_packages.items():
        if not isinstance(entry, dict):
            return None
        if entry.get("optional") and not (
            _platform_matches(entry.get("os"), os_name)
            and _platform_matches(entry.get("cpu"), arch)
        ):
            continue
        if _optional_ancestor_skipped(name, expected_packages, installed_packages, arch, os_name):
            continue
        current = installed_packages.get(name)
        if not isinstance(current, dict):
            return False
        if current.get("version") != entry.get("version"):
            return False
        if current.get("integrity") != entry.get("integrity"):
            return False

    for name in installed_packages:
        if name not in expected_packages:
            return False
    return True


def _manager_executable(manager: str) -> str:
    executable = _which(manager)
    if not executable:
        raise ReviewEnvError(f"{manager} is not on PATH")
    return executable


def _install_command(repo: Path, manager: str) -> list[str]:
    executable = _manager_executable(manager)
    has_lock = any((repo / name).is_file() for name in NODE_LOCKFILES)
    if manager == "npm":
        args = ["ci"] if (repo / "package-lock.json").is_file() else ["install"]
        return [executable, *args, "--prefer-offline", "--no-audit", "--no-fund"]
    if manager == "pnpm":
        args = ["install"]
        if has_lock:
            args.append("--frozen-lockfile")
        return [executable, *args, "--prefer-offline"]
    if manager == "bun":
        args = ["install"]
        if has_lock:
            args.append("--frozen-lockfile")
        return [executable, *args]
    version = _run([executable, "--version"])
    major = 0
    if version.returncode == 0:
        match = re.match(r"(\d+)", version.stdout.strip())
        major = int(match.group(1)) if match else 0
    if major >= 2:
        return [executable, "install", "--immutable"]
    return [executable, "install", "--frozen-lockfile", "--prefer-offline", "--non-interactive"]


def _install_env(base: dict[str, str]) -> dict[str, str]:
    env = dict(base)
    env["CI"] = "1"
    env["HUSKY"] = "0"
    env.pop("NODE_ENV", None)
    for name in ("npm_config_omit", "npm_config_only", "npm_config_production"):
        env.pop(name, None)
    return env


def _has_lifecycle_scripts(repo: Path) -> bool:
    manifest = repo / "package.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    if not isinstance(scripts, dict):
        return False
    return any(name in scripts for name in ("preinstall", "install", "postinstall", "prepare"))


def _copy_node_inputs(repo: Path, target: Path) -> None:
    inputs = set(node_files(repo))
    for relative in inputs:
        source = repo / relative
        destination = target / relative
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    # A lifecycle script may read these; they are not part of the dependency
    # key, so changing a script does not force a reinstall.
    if _has_lifecycle_scripts(repo):
        for name in NODE_COPY_DIRS:
            source = repo / name
            if name not in inputs and source.is_dir():
                shutil.copytree(source, target / name, dirs_exist_ok=True)


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle: int | None = None
    try:
        handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0
        if age > 7200:
            path.unlink(missing_ok=True)
            handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        else:
            raise ReviewEnvError(
                f"another review_env install is running for this key (lock: {path})"
            ) from None
    try:
        if handle is not None:
            os.write(handle, str(os.getpid()).encode("ascii"))
        yield
    finally:
        if handle is not None:
            os.close(handle)
            path.unlink(missing_ok=True)


def _install_node_cache(worktree: Path, cache_dir: Path, manager: str, key: str) -> None:
    if _is_dir_link(cache_dir):
        raise ReviewEnvError(f"refusing to reuse or replace a link at {cache_dir}")
    marker = _read_marker(cache_dir)
    if cache_dir.exists() and marker is None:
        raise ReviewEnvError(
            f"refusing to reuse or replace an unowned cache directory: {cache_dir}"
        )
    if marker is not None and marker.get("key") != key:
        raise ReviewEnvError(f"cache directory {cache_dir} belongs to a different dependency key")
    with _lock(cache_dir.with_name(f".{cache_dir.name}.lock")):
        if (cache_dir / "node_modules").is_dir() and _marker_ready(cache_dir):
            return
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            _copy_node_inputs(worktree, cache_dir)
            _write_marker(
                cache_dir,
                key,
                state="installing",
                ecosystem="node",
                manager=manager,
                source=str(worktree),
            )
            command = _install_command(worktree, manager)
            if not _json_mode:
                print(
                    f"review_env: installing {manager} dependencies for {_short(key)} "
                    f"into {cache_dir}",
                    file=sys.stderr,
                )
            proc = _run(
                command, cwd=cache_dir, env=_install_env(os.environ), timeout=INSTALL_TIMEOUT
            )
            if proc.returncode != 0 or not (cache_dir / "node_modules").is_dir():
                detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                tail = "\n".join(detail[-20:]) if detail else f"{manager} exited {proc.returncode}"
                raise ReviewEnvError(f"{manager} install failed in {cache_dir}:\n{tail}")
            _write_marker(
                cache_dir,
                key,
                state="ready",
                ecosystem="node",
                manager=manager,
                source=str(worktree),
            )
        except BaseException:
            # A half-built directory would block the next attempt as "unowned";
            # remove the directory this call created.
            shutil.rmtree(cache_dir, ignore_errors=True)
            raise


# --- python ----------------------------------------------------------------


def python_files(repo: Path) -> tuple[str, ...]:
    if not (repo / "pyproject.toml").is_file() or not (repo / "poetry.lock").is_file():
        return ()
    return tuple(name for name in PYTHON_FILES if (repo / name).is_file())


def _venv_python(env: Path) -> Path | None:
    candidates = (
        env / "Scripts" / "python.exe",
        env / "bin" / "python",
        env / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _venv_bin(env: Path) -> Path:
    return env / ("Scripts" if os.name == "nt" else "bin")


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip("-").lower()


def _poetry_project(repo: Path) -> tuple[str, bool]:
    """Return ``(name, package_mode)`` from ``[tool.poetry]``.

    ``package-mode = false`` means Poetry never installs the root project, so
    reusing the main environment cannot make a worktree import the main source.
    """
    path = repo / "pyproject.toml"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return "", True
    if tomllib is not None:
        try:
            data = tomllib.loads(raw)
            poetry = data.get("tool", {}).get("poetry", {})
            project = data.get("project", {})
            if isinstance(poetry, dict):
                name = poetry.get("name") or (
                    project.get("name") if isinstance(project, dict) else None
                )
                return str(name or ""), bool(poetry.get("package-mode", True))
        except Exception:
            pass
    name_match = re.search(r'(?ms)^\[tool\.poetry\].*?^name\s*=\s*"([^"]+)"', raw)
    if name_match is None:
        name_match = re.search(r'(?ms)^\[project\].*?^name\s*=\s*"([^"]+)"', raw)
    package_mode = not re.search(r"(?m)^package-mode\s*=\s*false\s*$", raw)
    return (name_match.group(1) if name_match else ""), package_mode


def _venv_site_packages(env: Path) -> Path | None:
    if os.name == "nt":
        candidate = env / "Lib" / "site-packages"
        return candidate if candidate.is_dir() else None
    for candidate in env.glob("lib/python*/site-packages"):
        if candidate.is_dir():
            return candidate
    fallback = env / "lib" / "site-packages"
    return fallback if fallback.is_dir() else None


def _venv_has_project_install(env: Path, project: str, main: Path) -> bool:
    """True when *env* has the root project installed from *main*.

    A shared environment with the project installed (editable or as a wheel)
    would make a worktree import the main checkout's source. The cache
    environment is built with ``--no-root`` precisely to avoid that.
    """
    site = _venv_site_packages(env)
    if site is None or not project:
        return False
    canonical = _canonical_name(project)
    for metadata in list(site.glob("*.dist-info/METADATA")) + list(
        site.glob("*.egg-info/PKG-INFO")
    ):
        try:
            for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines()[:20]:
                if line.startswith("Name: "):
                    if _canonical_name(line[6:]) == canonical:
                        return True
                    break
        except OSError:
            continue
    for link in site.glob("*.egg-link"):
        try:
            if str(main).lower() in link.read_text(encoding="utf-8", errors="replace").lower():
                return True
        except OSError:
            continue
    for path_file in site.glob("*.pth"):
        try:
            if str(main).lower() in path_file.read_text(encoding="utf-8", errors="replace").lower():
                return True
        except OSError:
            continue
    return False


def _pyvenv_version(env: Path) -> str | None:
    config = env / "pyvenv.cfg"
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^version\s*=\s*(\d+)\.(\d+)", text, re.MULTILINE)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    match = re.search(r"^version_info\s*=\s*(\d+)\.(\d+)", text, re.MULTILINE)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    return None


def _python_version(repo: Path, base_env: Path | None) -> str:
    version_file = repo / ".python-version"
    if version_file.is_file():
        try:
            raw = version_file.read_text(encoding="utf-8").strip().splitlines()
        except OSError:
            raw = []
        if raw:
            match = re.match(r"(\d+)\.(\d+)", raw[0])
            if match:
                return f"{match.group(1)}.{match.group(2)}"
    if base_env is not None:
        version = _pyvenv_version(base_env)
        if version:
            return version
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def find_poetry_env(base: Path) -> Path | None:
    """Return the Poetry environment already installed for *base*.

    ``poetry env info --path`` is preferred over ``poetry run`` because the
    latter creates an environment as a side effect, and preparing a review
    environment must not touch the checkout it is meant to observe.
    """
    for name in (".venv", "venv"):
        env = base / name
        if _venv_python(env) is not None:
            return env
    executable = _which("poetry")
    if not executable:
        return None
    proc = _run([executable, "env", "info", "--path"], cwd=base)
    if proc.returncode != 0:
        return None
    for line in reversed(proc.stdout.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_dir() and _venv_python(path) is not None:
            return path
    return None


def _poetry_install(repo: Path, env: Path) -> None:
    executable = _which("poetry")
    if not executable:
        raise ReviewEnvError("poetry is not on PATH")
    child_env = dict(os.environ)
    child_env["VIRTUAL_ENV"] = str(env)
    child_env["POETRY_VIRTUALENVS_CREATE"] = "false"
    child_env["POETRY_NO_INTERACTION"] = "1"
    child_env["PYTHONUTF8"] = "1"
    child_env["PATH"] = str(_venv_bin(env)) + os.pathsep + child_env.get("PATH", "")
    for name in (
        "PYTHONHOME",
        "PYTHONPATH",
        "POETRY_WITH",
        "POETRY_WITHOUT",
        "POETRY_EXTRAS",
        "POETRY_ONLY",
    ):
        child_env.pop(name, None)
    proc = _run(
        [executable, "install", "--no-root", "--no-interaction", "--no-ansi"],
        cwd=repo,
        env=child_env,
        timeout=INSTALL_TIMEOUT,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = "\n".join(detail[-20:]) if detail else f"poetry exited {proc.returncode}"
        raise ReviewEnvError(f"poetry install failed in {repo}:\n{tail}")


def _create_python_env(
    worktree: Path,
    env: Path,
    version: str,
    key: str,
    *,
    base_env: Path | None,
) -> None:
    if _is_dir_link(env):
        raise ReviewEnvError(f"refusing to reuse or replace a link at {env}")
    marker = _read_marker(env)
    if env.exists() and marker is None:
        raise ReviewEnvError(f"refusing to replace an unowned environment: {env}")
    if marker is not None and marker.get("key") != key:
        raise ReviewEnvError(f"environment {env} belongs to a different dependency key")
    with _lock(env.with_name(f".{env.name}.lock")):
        if _venv_python(env) is not None and _marker_ready(env):
            return
        if env.exists():
            shutil.rmtree(env, ignore_errors=True)
        env.parent.mkdir(parents=True, exist_ok=True)
        try:
            creator = _which("uv")
            if creator:
                proc = _run(
                    [creator, "venv", str(env), "--python", version, "--allow-existing"],
                    timeout=300,
                )
            else:
                python = _venv_python(base_env) if base_env is not None else None
                python = python or Path(sys.executable)
                proc = _run([str(python), "-m", "venv", str(env)], timeout=300)
            if proc.returncode != 0 or _venv_python(env) is None:
                detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                tail = "\n".join(detail[-20:]) if detail else "the creator exited non-zero"
                raise ReviewEnvError(f"cannot create the virtualenv at {env}:\n{tail}")
            _write_marker(
                env,
                key,
                state="installing",
                ecosystem="python",
                manager="poetry",
                source=str(worktree),
                python=version,
            )
            if not _json_mode:
                print(f"review_env: installing Poetry dependencies into {env}", file=sys.stderr)
            _poetry_install(worktree, env)
        except BaseException:
            shutil.rmtree(env, ignore_errors=True)
            raise
        _write_marker(
            env,
            key,
            state="ready",
            ecosystem="python",
            manager="poetry",
            source=str(worktree),
            python=version,
        )


# --- cache and markers -----------------------------------------------------


_json_mode = False


def _default_cache() -> Path:
    if CACHE_ENV in os.environ and os.environ[CACHE_ENV].strip():
        return Path(os.environ[CACHE_ENV])
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches"
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return root / "gryphon" / "review-envs"


def _slug(repo: Path) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", repo.name.lower()).strip("-") or "repo"
    digest = hashlib.sha256(str(repo).lower().encode("utf-8")).hexdigest()[:8]
    return f"{name}-{digest}"


def _resolve_cache(value: str | None, repo: Path, main: Path) -> Path:
    cache = Path(value).expanduser().resolve() if value else _default_cache().resolve()
    for boundary in (repo, main):
        if cache == boundary or boundary in cache.parents:
            raise ReviewEnvError(f"cache directory must live outside the repository: {cache}")
    return cache


def _read_marker(path: Path) -> dict[str, Any] | None:
    marker = path / MARKER
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != MARKER_VERSION:
        return None
    return payload


def _write_marker(path: Path, key: str, **extra: Any) -> None:
    payload = {"v": MARKER_VERSION, "key": key, "created": int(time.time()), **extra}
    try:
        (path / MARKER).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        raise ReviewEnvError(f"cannot write the environment marker in {path}: {exc}") from exc


def _marker_ready(path: Path) -> bool:
    payload = _read_marker(path)
    return payload is not None and payload.get("state", "ready") == "ready"


def _remove_marker(path: Path) -> None:
    try:
        (path / MARKER).unlink(missing_ok=True)
    except OSError:
        pass


def _cache_root(cache: Path, main: Path) -> Path:
    return cache / _slug(main)


def _python_cache_dir(cache: Path, main: Path, key: str, version: str) -> Path:
    return _cache_root(cache, main) / "python" / f"{key[:16]}-py{version}"


def _node_cache_dir(cache: Path, main: Path, key: str) -> Path:
    return _cache_root(cache, main) / "node" / key[:16]


# --- planning --------------------------------------------------------------


def _changed_on_branch(repo: Path, base: str | None, files: tuple[str, ...]) -> bool | None:
    if not base or not files:
        return None
    proc = _run(["git", "-C", str(repo), "diff", "--quiet", f"{base}...HEAD", "--", *files])
    if proc.returncode == 0:
        return False
    if proc.returncode == 1:
        return True
    return None


def plan_python(
    repo: Path,
    main: Path,
    base: str | None,
    cache: Path,
    *,
    fast: bool,
) -> dict[str, Any] | None:
    files = python_files(repo)
    if not files:
        return None
    main_files = python_files(main)
    base_env = find_poetry_env(main)
    version = _python_version(repo, base_env)
    key = dependency_key(repo, files, ecosystem="python", manager="poetry", runtime=f"py{version}")
    # The worktree key uses the version the project declares; the base key must
    # use the version the base environment *is*. Otherwise a .python-version
    # pin of 3.12 would let a 3.14 base environment pass as a match.
    base_version = _pyvenv_version(base_env) if base_env is not None else None
    base_version = base_version or _python_version(main, None)
    base_key = None
    if main_files:
        base_key = dependency_key(
            main, main_files, ecosystem="python", manager="poetry", runtime=f"py{base_version}"
        )
    base_marker = _read_marker(base_env) if base_env is not None else None
    base_is_trustworthy = base_marker is None or base_marker.get("key") == base_key
    base_python = _pyvenv_version(base_env) if base_env is not None else None
    base_is_link = _is_dir_link(base_env) if base_env is not None else False

    warning = None
    root_installed = False
    project = ""
    if base_env is not None and repo != main and (base_key == key or fast):
        project, package_mode = _poetry_project(repo)
        if package_mode and project:
            root_installed = _venv_has_project_install(base_env, project, main)

    fast_allowed = (
        fast
        and base_env is not None
        and base_key != key
        and base_python == version
        and not root_installed
        and not base_is_link
    )
    if base_env is not None and base_key == key and base_is_trustworthy and not root_installed:
        action = "reused-base"
        env: Path | None = base_env
    elif fast_allowed:
        action = "will-sync-base"
        env = base_env
    else:
        env = _python_cache_dir(cache, main, key, version)
        if _venv_python(env) is not None and _marker_ready(env):
            action = "reused-cache"
        else:
            action = "will-create"
        if root_installed:
            warning = (
                f"the base Poetry environment has {project} installed from the main "
                "checkout; a cache environment is used so tests import this worktree's "
                "source"
            )
            if fast:
                warning = "--fast was ignored: " + warning
        elif fast and base_env is not None and base_is_link:
            warning = (
                "--fast was ignored: the base Poetry environment is a link; installing "
                "through it would mutate its target"
            )
        elif fast and base_env is not None and base_key != key and base_python != version:
            warning = (
                "--fast was ignored: the base environment runs Python "
                f"{base_python or 'unknown'}, but this worktree asks for {version}"
            )
    interpreter = _venv_python(env) if env is not None else None
    return {
        "ecosystem": "python",
        "manager": "poetry",
        "key": key,
        "base_key": base_key,
        "python_version": version,
        "changed_on_branch": _changed_on_branch(repo, base, files),
        "action": action,
        "env": env,
        "interpreter": interpreter,
        "files": files,
        "warning": warning,
    }


def _node_key_for(path: Path, manager: str) -> str | None:
    files = node_files(path)
    if not files or node_manager(path) != manager:
        return None
    return dependency_key(path, files, ecosystem="node", manager=manager, runtime=_node_version())


def plan_node(
    repo: Path,
    main: Path,
    base: str | None,
    cache: Path,
    *,
    force: bool,
) -> dict[str, Any] | None:
    files = node_files(repo)
    if not files:
        return None
    manager = node_manager(repo)
    if manager is None:
        return None
    key = dependency_key(repo, files, ecosystem="node", manager=manager, runtime=_node_version())
    main_files = node_files(main)
    main_manager = node_manager(main)
    main_key = None
    if main_files and main_manager == manager:
        main_key = dependency_key(
            main, main_files, ecosystem="node", manager=manager, runtime=_node_version()
        )
    main_modules = main / "node_modules"
    cache_dir = _node_cache_dir(cache, main, key)
    cache_modules = cache_dir / "node_modules"
    current = repo / "node_modules"
    state = _describe_node_modules(current)
    warning = None
    action: str
    link_target: Path | None = None
    desired = _desired_link(main_key, key, main_modules, cache_modules)

    current_real = state["kind"] == "dir" and _npm_tree_current(repo) is True
    if _is_monorepo(repo) and not current_real:
        action = "monorepo"
        warning = (
            "workspace monorepo: a shared node_modules would resolve workspace packages "
            "from the checkout that installed it, not this worktree. Install dependencies "
            "in the worktree instead."
        )
    elif repo == main:
        if state["kind"] == "link":
            source = state.get("target")
            source_key = _node_key_for(source.parent, manager) if isinstance(source, Path) else None
            if source_key == key:
                action = "kept-existing"
                link_target = source
            else:
                action = "will-install-cache"
                link_target = cache_modules
        elif state["kind"] == "dir":
            verified = _npm_tree_current(repo) if manager == "npm" else None
            if verified is False:
                action = "will-install-in-place"
                warning = (
                    "the existing node_modules does not match package-lock; "
                    "installing this checkout's dependencies in place"
                )
            else:
                action = "kept-existing"
                if verified is None and manager == "npm":
                    warning = (
                        "existing node_modules was not verified against the lock "
                        "(no hidden package-lock); reinstall if the lock changed"
                    )
        else:
            action = "will-install-in-place"
    elif state["kind"] == "link":
        if desired is not None and state.get("target") == desired:
            action = "reused-link"
            link_target = desired
        elif desired is not None:
            action = "will-relink"
            link_target = desired
        else:
            action = "will-install-cache"
            link_target = cache_modules
    elif state["kind"] == "dir":
        verified = _npm_tree_current(repo) if manager == "npm" else None
        if verified is True:
            action = "kept-existing"
        elif force:
            action = "will-replace-real"
            link_target = desired if desired is not None else cache_modules
        else:
            action = "unsafe-real"
            warning = (
                "a real node_modules exists and could not be verified against the lock; "
                "re-run with --force to move it aside and link the cached tree"
            )
    elif state["kind"] == "other":
        action = "will-replace-real" if force else "unsafe-real"
        link_target = desired if desired is not None else cache_modules
        if not force:
            warning = (
                "node_modules exists but is not a directory; re-run with --force to move "
                "it aside and link the cached tree"
            )
    elif desired is not None and desired == main_modules.resolve() and main_modules.is_dir():
        action = "will-link-main"
        link_target = desired
    elif desired is not None:
        action = "will-link-cache"
        link_target = desired
    else:
        action = "will-install-cache"
        link_target = cache_modules

    if state["kind"] == "dir" and action == "kept-existing" and manager != "npm":
        warning = (
            "existing node_modules was not verified against the lock (only npm exposes a "
            "hidden lockfile); reinstall it if the lock changed"
        )
    return {
        "ecosystem": "node",
        "manager": manager,
        "key": key,
        "base_key": main_key,
        "changed_on_branch": _changed_on_branch(repo, base, files),
        "action": action,
        "node_modules": current,
        "link_target": link_target,
        "files": files,
        "warning": warning,
    }


def _desired_link(
    main_key: str | None, key: str, main_modules: Path, cache_modules: Path
) -> Path | None:
    if main_key == key and main_modules.is_dir():
        return main_modules.resolve()
    if cache_modules.is_dir() and _marker_ready(cache_modules.parent):
        return cache_modules.resolve()
    return None


def build_plan(
    repo: Path,
    main: Path,
    base: str | None,
    cache: Path,
    *,
    fast: bool,
    force: bool,
) -> dict[str, Any]:
    python = plan_python(repo, main, base, cache, fast=fast)
    node = plan_node(repo, main, base, cache, force=force)
    notes: list[str] = []
    if python is None and node is None:
        notes.append("nothing to prepare: no Poetry or Node dependency manifest at the root")
    if node is not None:
        if not any((repo / name).is_file() for name in NODE_LOCKFILES):
            notes.append(
                "no Node lockfile: the cache key covers package.json only, so dependency "
                "versions are not reproducible"
            )
        if node["action"] in {
            "will-link-main",
            "will-link-cache",
            "will-relink",
            "will-replace-real",
            "reused-link",
        }:
            notes.append(
                "node_modules is shared through a link: do not run npm/pnpm/yarn install through it"
            )
        if node["action"] == "will-install-cache":
            notes.append(
                "the dependency key changed; the first worktree at this state pays one "
                "install, the rest reuse it"
            )
    if python is not None and python["action"] == "will-create":
        notes.append(
            "one Poetry environment is cached per lock state and shared across worktrees; "
            "the first one pays the install"
        )
    if python is not None and python["action"] == "will-sync-base":
        notes.append(
            "--fast mutates the main checkout's Poetry environment; run "
            f"`review_env.py restore --repo {main}` when the review ends"
        )
    return {
        "repo": repo,
        "main_checkout": main,
        "base": base,
        "python": python,
        "node": node,
        "notes": notes,
    }


# --- preparing -------------------------------------------------------------


def _prepare_python(plan: dict[str, Any], cache: Path, main: Path, *, fast: bool) -> None:
    action = plan["action"]
    if action in {"reused-base", "reused-cache"}:
        return
    if action == "will-sync-base":
        repo = plan["_repo"]
        env = plan["env"]
        with _lock(env.with_name(f".{env.name}.gryphon-fast.lock")):
            _poetry_install(repo, env)
            _write_marker(
                env,
                plan["key"],
                ecosystem="python",
                manager="poetry",
                base_key=plan["base_key"],
                fast=True,
                source=str(repo),
            )
        plan["action"] = "synced-base"
        plan["interpreter"] = _venv_python(env)
        return
    if action == "will-create":
        repo = plan["_repo"]
        env = plan["env"]
        base_env = find_poetry_env(main)
        _create_python_env(
            repo,
            env,
            plan["python_version"],
            plan["key"],
            base_env=base_env,
        )
        plan["action"] = "created-cache"
        plan["interpreter"] = _venv_python(env)
        return
    raise ReviewEnvError(f"unknown python action: {action}")


def _prepare_node(plan: dict[str, Any], cache: Path, main: Path, *, force: bool) -> None:
    action = plan["action"]
    if action in {"kept-existing", "reused-link", "monorepo"}:
        return
    repo = plan["_repo"]
    if action == "unsafe-real":
        raise UnsafeStateError(
            f"a real node_modules exists at {repo / 'node_modules'}; re-run with --force "
            "to move it aside and link the cached tree"
        )
    if action == "will-install-in-place":
        manager = plan["manager"]
        command = _install_command(repo, manager)
        if not _json_mode:
            print(f"review_env: installing {manager} dependencies in {repo}", file=sys.stderr)
        proc = _run(command, cwd=repo, env=_install_env(os.environ), timeout=INSTALL_TIMEOUT)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            tail = "\n".join(detail[-20:]) if detail else f"{manager} exited {proc.returncode}"
            raise ReviewEnvError(f"{manager} install failed in {repo}:\n{tail}")
        plan["action"] = "installed-in-place"
        return

    current = repo / "node_modules"
    target = plan.get("link_target")
    if action == "will-install-cache":
        cache_dir = _node_cache_dir(cache, main, plan["key"])
        _install_node_cache(repo, cache_dir, plan["manager"], plan["key"])
        target = cache_dir / "node_modules"
        plan["link_target"] = target
    elif action == "will-replace-real":
        cache_dir = _node_cache_dir(cache, main, plan["key"])
        cache_modules = (cache_dir / "node_modules").resolve()
        target_is_cache = target is not None and target.resolve() == cache_modules
        cache_ready = cache_modules.is_dir() and _marker_ready(cache_dir)
        if target is None or not target.is_dir() or (target_is_cache and not cache_ready):
            _install_node_cache(repo, cache_dir, plan["manager"], plan["key"])
            target = cache_dir / "node_modules"
            plan["link_target"] = target
    if target is None:
        raise ReviewEnvError("no link target was planned")

    if _is_dir_link(current):
        _remove_dir_link(current)
    elif current.exists():
        if not force:
            raise UnsafeStateError(
                f"refusing to replace the real path at {current}; re-run with --force"
            )
        plan["backup"] = _backup_real_dir(current)
    _make_dir_link(current, target)
    plan["action"] = {
        "will-link-main": "linked-main",
        "will-link-cache": "linked-cache",
        "will-install-cache": "linked-cache",
        "will-relink": "relinked",
        "will-replace-real": "replaced-real",
    }.get(action, action)


# --- restore ---------------------------------------------------------------


def restore(main: Path, *, as_json: bool) -> int:
    base_env = find_poetry_env(main)
    if base_env is None:
        raise ReviewEnvError(f"no Poetry environment found for {main}")
    if _is_dir_link(base_env):
        raise ReviewEnvError(
            f"refusing to restore through a link at {base_env}; it would mutate its target"
        )
    marker = _read_marker(base_env)
    if marker is None or marker.get("ecosystem") != "python" or not marker.get("fast"):
        if as_json:
            print(
                json.dumps(
                    {
                        "restored": False,
                        "env": str(base_env),
                        "main_checkout": str(main),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"review_env: nothing to restore in {base_env}")
        return 0
    with _lock(base_env.with_name(f".{base_env.name}.gryphon-fast.lock")):
        _poetry_install(main, base_env)
        _remove_marker(base_env)
    payload = {"restored": True, "env": str(base_env), "main_checkout": str(main)}
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"review_env: restored {base_env} to the dependencies of {main}")
    return 0


# --- rendering -------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item) for key, item in value.items() if not str(key).startswith("_")
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _render(plan: dict[str, Any], action: str) -> str:
    lines = [f"review_env: {action} {plan['repo']}"]
    lines.append(f"main checkout: {plan['main_checkout']}")
    if plan["base"]:
        lines.append(f"base: {plan['base']}")
    python = plan.get("python")
    if python is not None:
        changed = _changed_label(python.get("changed_on_branch"))
        lines.append(
            f"python/poetry  key {_short(python['key'])}  base {_short(python['base_key'] or '')}  "
            f"{python['action']}{changed}"
        )
        if python.get("interpreter"):
            lines.append(f"  interpreter: {python['interpreter']}")
    node = plan.get("node")
    if node is not None:
        changed = _changed_label(node.get("changed_on_branch"))
        base_key = _short(node["base_key"] or "")
        lines.append(
            f"node/{node['manager']}  key {_short(node['key'])}  base {base_key}  "
            f"{node['action']}{changed}"
        )
        if node.get("node_modules"):
            target = node.get("link_target")
            suffix = f" -> {target}" if target else ""
            lines.append(f"  node_modules: {node['node_modules']}{suffix}")
        if node.get("backup"):
            lines.append(f"  previous node_modules moved to: {node['backup']}")
    for note in plan.get("notes", []):
        lines.append(f"note: {note}")
    for entry in (python, node):
        if entry and entry.get("warning"):
            lines.append(f"warning: {entry['warning']}")
    return "\n".join(lines)


def _changed_label(value: bool | None) -> str:
    if value is True:
        return " (deps changed on branch)"
    if value is False:
        return " (deps unchanged on branch)"
    return ""


# --- CLI -------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reuse dependency environments across review worktrees, keyed by the hash "
            "of the dependency inputs."
        )
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=("plan", "prepare", "restore"),
        default="prepare",
        help="plan (no writes), prepare (default), or restore a --fast environment",
    )
    parser.add_argument("--repo", default=".", help="Worktree or checkout (default: .)")
    parser.add_argument("--base", default=None, help="Base ref, for the changed-on-branch note")
    parser.add_argument("--cache-dir", default=None, help="Cache root (outside the repository)")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Move a real node_modules aside before linking (it is renamed, never deleted)",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Python only: sync the delta into the main checkout's Poetry environment "
            "instead of building an isolated one (mutates it; use restore afterwards)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    global _json_mode
    args = _parser().parse_args(argv)
    _json_mode = bool(args.json)
    try:
        repo = resolve_repo(Path(args.repo))
        main_checkout = find_main_checkout(repo)
        if args.action == "restore":
            return restore(main_checkout, as_json=args.json)
        cache = _resolve_cache(args.cache_dir, repo, main_checkout)
        plan = build_plan(
            repo,
            main_checkout,
            args.base,
            cache,
            fast=args.fast,
            force=args.force,
        )
        if args.action == "plan":
            code = 0 if (plan["python"] or plan["node"]) else 2
        else:
            if plan["python"] is not None:
                plan["python"]["_repo"] = repo
            if plan["node"] is not None:
                plan["node"]["_repo"] = repo
            code = 0 if (plan["python"] or plan["node"]) else 2
            try:
                if plan["python"] is not None:
                    _prepare_python(plan["python"], cache, main_checkout, fast=args.fast)
                if plan["node"] is not None:
                    _prepare_node(plan["node"], cache, main_checkout, force=args.force)
            except UnsafeStateError as exc:
                if args.json:
                    print(json.dumps(_jsonable(plan), indent=2, ensure_ascii=False))
                else:
                    print(_render(plan, "prepare"))
                print(f"review_env: {exc}", file=sys.stderr)
                return 3
            if any(
                entry is not None and entry.get("action") in {"unsafe-real", "monorepo"}
                for entry in (plan["python"], plan["node"])
            ):
                code = (
                    3
                    if any(
                        entry is not None and entry.get("action") == "unsafe-real"
                        for entry in (plan["python"], plan["node"])
                    )
                    else 2
                )
        if args.json:
            print(json.dumps(_jsonable(plan), indent=2, ensure_ascii=False))
        else:
            print(_render(plan, args.action))
        return code
    except ReviewEnvError as exc:
        print(f"review_env: {exc}", file=sys.stderr)
        return 4
    except OSError as exc:
        print(f"review_env: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())

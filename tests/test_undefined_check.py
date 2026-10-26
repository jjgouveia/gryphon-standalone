"""Contract tests for scripts/check_undefined.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_undefined  # noqa: E402

RESOLVED = check_undefined.resolve_ruff(ROOT)
pytestmark = pytest.mark.skipif(
    RESOLVED is None, reason="ruff is not installed in this environment"
)

FIXED = '''"""Module docstring."""

from itertools import chain


def build(items):
    if not items:
        return []
    return list(chain.from_iterable(items))
'''

BROKEN = '''"""Module docstring."""


def build(items):
    if not items:
        return []
    return list(chain.from_iterable(items))
'''


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _repo(tmp_path: Path, base_text: str, head_text: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "reviewer@example.com")
    _git(repo, "config", "user.name", "reviewer")
    target = repo / "pkg" / "mod.py"
    target.write_text(base_text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    target.write_text(head_text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "head", "--allow-empty")
    return repo


def _collect(repo: Path, files: list[str]) -> dict:
    assert RESOLVED is not None
    ruff, version = RESOLVED
    return check_undefined.collect(repo, "HEAD~1", files, ruff, version)


def test_missing_import_left_by_the_diff_is_introduced(tmp_path: Path) -> None:
    """The motivating case: the import goes, the surviving call does not."""
    repo = _repo(tmp_path, FIXED, BROKEN)

    result = _collect(repo, ["pkg/mod.py"])

    assert result["introduced"] == 1
    (finding,) = result["findings"]
    assert finding["symbol"] == "chain"
    assert finding["code"] == "F821"
    assert finding["origin"] == "introduced"


def test_hit_that_already_existed_at_base_does_not_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path, BROKEN, BROKEN)

    result = _collect(repo, ["pkg/mod.py"])

    assert result["introduced"] == 0
    (finding,) = result["findings"]
    assert finding["origin"] == "pre_existing"


def test_non_python_files_are_reported_as_skipped(tmp_path: Path) -> None:
    repo = _repo(tmp_path, FIXED, BROKEN)

    result = _collect(repo, ["app.ts"])

    assert result["findings"] == []
    assert result["skipped"] == [{"path": "app.ts", "reason": "not Python"}]


def test_files_default_to_the_base_diff(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path, FIXED, BROKEN)

    code = check_undefined.main(["--repo", str(repo), "--base", "HEAD~1", "--json"])

    assert code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["checked"] == ["pkg/mod.py"]
    assert result["introduced"] == 1


def test_clean_file_exits_zero(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path, FIXED, FIXED)

    code = check_undefined.main(["--repo", str(repo), "--base", "HEAD~1", "--files", "pkg/mod.py"])

    assert code == 0
    assert "result: 0 introduced" in capsys.readouterr().out


def test_missing_checker_exits_two(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = _repo(tmp_path, FIXED, BROKEN)
    monkeypatch.setattr(check_undefined, "resolve_ruff", lambda *args, **kwargs: None)

    code = check_undefined.main(["--repo", str(repo), "--base", "HEAD~1", "--files", "pkg/mod.py"])

    assert code == 2
    assert "no ruff found" in capsys.readouterr().err

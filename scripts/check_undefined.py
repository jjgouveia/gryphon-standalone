#!/usr/bin/env python3
"""Find undefined names the change set introduced.

The blind spot this closes is the bug that is not in a hunk. A commit deletes
``from itertools import chain`` together with the one caller it noticed, and
the calls that survive sit outside every diff line: ``gh pr diff`` cannot show
them, the module still imports, and the failure only appears when that branch
runs. A reviewer reading the diff sees a coherent story.

Linting the *files* the change touches finds it. Each hit is classified
against the base version of the same file, because a repo can carry years of
legacy undefined names and a review that blocks on those punishes the wrong
author:

    introduced    the name resolved at base and does not at head
    pre_existing  already broken at base; inherited debt, never a merge gate
    unknown       no base version was available to compare against

Only Python is checked (``ruff --select F821,F811``). Other languages are
reported as skipped rather than silently passed.

Exit codes:
    0  nothing introduced (clean, or only pre-existing hits)
    1  at least one introduced hit
    2  no checker could run, or nothing could be checked
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CHECKS = "F821,F811"
PY_SUFFIXES = {".py", ".pyi"}
_SYMBOL = re.compile(r"`([^`]+)`")
_PROBE_TIMEOUT = 20
_CAPTURE_TEXT = {"capture_output": True, "text": True, "encoding": "utf-8"}


class CheckError(RuntimeError):
    """The checker could not run or returned something unreadable."""


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    column: int
    code: str
    symbol: str
    message: str
    origin: str = "unknown"


def _ruff_candidates(repo: Path, override: str | None) -> list[list[str]]:
    """Return ruff invocations to try, most specific first."""
    candidates: list[list[str]] = []
    if override:
        candidates.append([override])

    exe = "ruff.exe" if os.name == "nt" else "ruff"
    # The interpreter running this script is usually the review environment;
    # when pytest runs inside the venv, ruff lives beside it.
    candidates.append([str(Path(sys.executable).parent / exe)])
    for venv in (".venv", "venv"):
        candidates.append([str(repo / venv / "bin" / "ruff")])
        candidates.append([str(repo / venv / "Scripts" / "ruff.exe")])

    on_path = shutil.which("ruff")
    if on_path:
        candidates.append([on_path])

    # Poetry projects often keep ruff as a dev dependency without an in-tree
    # virtualenv. Ask for the env path first: `poetry run` would create one as
    # a side effect of a probe, which must not happen inside a reviewed repo.
    if shutil.which("poetry") and (repo / "pyproject.toml").is_file():
        try:
            info = subprocess.run(
                ["poetry", "env", "info", "--path"],
                cwd=repo,
                timeout=_PROBE_TIMEOUT,
                check=False,
                **_CAPTURE_TEXT,
            )
        except (OSError, subprocess.SubprocessError):
            info = None
        if info is not None and info.returncode == 0:
            candidates.append(["poetry", "run", "ruff"])
    return candidates


def resolve_ruff(repo: Path, override: str | None = None) -> tuple[list[str], str] | None:
    """Pick the first ruff invocation that answers ``--version``."""
    for command in _ruff_candidates(repo, override):
        try:
            proc = subprocess.run(
                [*command, "--version"],
                cwd=repo,
                timeout=_PROBE_TIMEOUT,
                check=False,
                **_CAPTURE_TEXT,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return command, proc.stdout.strip()
    return None


def _run_ruff(
    ruff: list[str],
    repo: Path,
    paths: list[Path],
    *,
    stdin_path: str | None = None,
    stdin_text: str | None = None,
) -> list[dict[str, Any]]:
    command = [
        *ruff,
        "check",
        "--select",
        CHECKS,
        "--output-format",
        "json",
        "--no-cache",
    ]
    if stdin_path is not None:
        command += ["--stdin-filename", stdin_path, "-"]
        proc = subprocess.run(
            command,
            cwd=repo,
            input=stdin_text or "",
            check=False,
            **_CAPTURE_TEXT,
        )
    else:
        command += [str(path) for path in paths]
        proc = subprocess.run(command, cwd=repo, check=False, **_CAPTURE_TEXT)

    if proc.returncode not in (0, 1):
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise CheckError(detail[0] if detail else f"ruff exited {proc.returncode}")
    try:
        payload = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise CheckError(f"ruff returned unreadable JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise CheckError("ruff returned an unexpected JSON document")
    return payload


def _relative(repo: Path, filename: str) -> str:
    try:
        return Path(filename).resolve().relative_to(repo).as_posix()
    except (ValueError, OSError):
        return Path(filename).as_posix()


def _hit(repo: Path, raw: dict[str, Any]) -> Hit:
    message = str(raw.get("message") or "").strip()
    location = raw.get("location") or {}
    match = _SYMBOL.search(message)
    return Hit(
        path=_relative(repo, str(raw.get("filename") or "")),
        line=int(location.get("row") or 0),
        column=int(location.get("column") or 0),
        code=str(raw.get("code") or ""),
        symbol=match.group(1) if match else message,
        message=message,
    )


def _base_symbols(
    ruff: list[str], repo: Path, base: str, paths: list[str]
) -> dict[str, set[tuple[str, str]] | None]:
    """Run the same check against each file's content at ``base``.

    ``None`` marks a file that did not exist at base, which makes every hit in
    it introduced.
    """
    symbols: dict[str, set[tuple[str, str]] | None] = {}
    for path in paths:
        proc = subprocess.run(
            ["git", "-C", str(repo), "show", f"{base}:{path}"],
            check=False,
            capture_output=True,
        )
        if proc.returncode != 0:
            symbols[path] = None
            continue
        text = proc.stdout.decode("utf-8", errors="replace")
        raws = _run_ruff(ruff, repo, [], stdin_path=path, stdin_text=text)
        symbols[path] = {(hit.code, hit.symbol) for hit in (_hit(repo, raw) for raw in raws)}
    return symbols


def _changed_files(repo: Path, base: str, head: str) -> list[str]:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            f"{base}...{head}",
        ],
        check=False,
        **_CAPTURE_TEXT,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise CheckError(detail[0] if detail else f"git diff exited {proc.returncode}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def collect(
    repo: Path,
    base: str,
    files: list[str],
    ruff: list[str],
    ruff_version: str,
) -> dict[str, Any]:
    """Lint ``files`` and classify every hit against ``base``."""
    checked: list[str] = []
    skipped: list[dict[str, str]] = []
    missing: list[str] = []

    for raw in files:
        path = Path(raw)
        if path.suffix not in PY_SUFFIXES:
            skipped.append({"path": raw, "reason": "not Python"})
        elif not (repo / path).is_file():
            missing.append(raw)
        else:
            checked.append(path.as_posix())

    hits: list[Hit] = []
    if checked:
        raws = _run_ruff(ruff, repo, [repo / rel for rel in checked])
        hits = [_hit(repo, raw) for raw in raws]

    base_map: dict[str, set[tuple[str, str]] | None] = {}
    if hits:
        base_map = _base_symbols(ruff, repo, base, sorted({hit.path for hit in hits}))

    classified: list[Hit] = []
    for hit in hits:
        known = base_map.get(hit.path)
        origin = (
            "unknown"
            if known is None
            else ("pre_existing" if (hit.code, hit.symbol) in known else "introduced")
        )
        classified.append(
            Hit(
                path=hit.path,
                line=hit.line,
                column=hit.column,
                code=hit.code,
                symbol=hit.symbol,
                message=hit.message,
                origin=origin,
            )
        )

    classified.sort(key=lambda hit: (hit.origin != "introduced", hit.path, hit.line))
    return {
        "base": base,
        "checker": {"name": "ruff", "version": ruff_version, "command": ruff},
        "checked": checked,
        "skipped": skipped,
        "missing": missing,
        "findings": [asdict(hit) for hit in classified],
        "introduced": sum(1 for hit in classified if hit.origin == "introduced"),
    }


def _render(result: dict[str, Any]) -> str:
    lines = [f"check_undefined: {result['checker']['version']} (base {result['base']})"]
    findings = result["findings"]
    introduced = [hit for hit in findings if hit["origin"] == "introduced"]
    inherited = [hit for hit in findings if hit["origin"] == "pre_existing"]
    unknown = [hit for hit in findings if hit["origin"] == "unknown"]

    if introduced:
        lines.append(f"introduced ({len(introduced)})")
        lines += [
            f"  {hit['path']}:{hit['line']}:{hit['column']} {hit['code']} "
            f"`{hit['symbol']}`: {hit['message']}"
            for hit in introduced
        ]
    if inherited:
        lines.append(f"pre_existing ({len(inherited)})")
        lines += [
            f"  {hit['path']}:{hit['line']}:{hit['column']} {hit['code']} `{hit['symbol']}`"
            for hit in inherited
        ]
    if unknown:
        lines.append(f"unknown ({len(unknown)})")
        lines += [
            f"  {hit['path']}:{hit['line']}:{hit['column']} {hit['code']} `{hit['symbol']}`"
            for hit in unknown
        ]

    if result["skipped"]:
        reasons: dict[str, int] = {}
        for item in result["skipped"]:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
        summary = ", ".join(f"{count} {reason}" for reason, count in reasons.items())
        lines.append(f"skipped: {summary}")
    if result["missing"]:
        lines.append(f"not in the working tree: {len(result['missing'])}")

    lines.append(f"result: {result['introduced']} introduced, {len(inherited)} pre-existing")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Lint the files a change touches for undefined names and classify "
            "each hit as introduced or pre-existing."
        )
    )
    parser.add_argument("--repo", default=".", help="Repository root (default: .)")
    parser.add_argument(
        "--base",
        required=True,
        help="Base ref to diff against and to read the pre-change state from",
    )
    parser.add_argument("--head", default="HEAD", help="Head ref used when --files is omitted")
    parser.add_argument(
        "--files",
        nargs="+",
        metavar="PATH",
        help="Changed paths (repo-relative). Omit to derive from git diff",
    )
    parser.add_argument("--ruff", default=None, help="Explicit ruff executable")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print(f"check_undefined: {repo} is not a git checkout", file=sys.stderr)
        return 2

    resolved = resolve_ruff(repo, args.ruff)
    if resolved is None:
        print(
            "check_undefined: no ruff found (tried --ruff, the venv and PATH)",
            file=sys.stderr,
        )
        return 2
    ruff, version = resolved

    try:
        files = args.files if args.files is not None else _changed_files(repo, args.base, args.head)
        result = collect(repo, args.base, files, ruff, version)
    except CheckError as exc:
        print(f"check_undefined: {exc}", file=sys.stderr)
        return 2

    if not result["checked"]:
        print(
            "check_undefined: nothing to check "
            f"({len(result['skipped'])} skipped, {len(result['missing'])} missing)",
            file=sys.stderr,
        )
        return 2

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(_render(result))
    return 1 if result["introduced"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

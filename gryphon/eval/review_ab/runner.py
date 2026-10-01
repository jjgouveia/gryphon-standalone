"""Run one review per arm with ``claude -p`` and record what it cost and found.

Each run streams ``stream-json`` to disk, so a timeout still leaves the
partial transcript. The record keeps cost, tokens, turns, duration, every
tool call and the structured findings, plus leak flags from an audit of the
tool calls: a reviewer that reached the network or a path outside its clone
could have read the PR's future, and that run must not be scored blind.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .cases import ReviewCase
from .defaults import (
    DEFAULT_BUDGET_USD,
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_S,
)
from .prompts import FINDINGS_SCHEMA, PROMPT_VERSION, build_prompt
from .sandbox import (
    ARMS,
    CLAUDE_MD_ARMS,
    REF_ARMS,
    ArmSandbox,
    NoMergeBaseError,
    _opaque_name,
    changed_files,
    prepare_arm,
    sandbox_kind,
)

logger = logging.getLogger(__name__)

BUILTIN_TOOLS = "Bash,Read,Grep,Glob"
DENIED_TOOLS = (
    "Bash(gh *)", "Bash(gh:*)", "Bash(curl *)", "Bash(wget *)",
    "Bash(git fetch *)", "Bash(git pull *)", "Bash(git push *)",
    "Bash(git clone *)", "Bash(git remote *)", "Bash(git ls-remote *)",
    "WebFetch", "WebSearch",
)
# Read-only shell commands both arms may run without a prompt. ``-p`` denies
# anything unlisted, and a denied compound command costs the reviewer turns.
# DENIED_TOOLS still wins over these (git fetch, gh, curl...).
READ_ONLY_BASH = tuple(
    f"Bash({c} *)"
    for c in (
        "git", "grep", "rg", "sed", "head", "tail", "cat", "ls", "find", "wc",
        "awk", "sort", "uniq", "cut", "diff", "cd", "echo",
    )
)
GRAPH_TOOL_PREFIX = "mcp__gryphon__"
REQUIRED_FIRST_CALLS = (
    f"{GRAPH_TOOL_PREFIX}get_minimal_context_tool",
    f"{GRAPH_TOOL_PREFIX}detect_changes_tool",
)

# Variables that make a nested claude think it runs inside another session.
_PARENT_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")


@dataclass(frozen=True)
class RunSettings:
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    max_budget_usd: float = DEFAULT_BUDGET_USD
    timeout_s: int = DEFAULT_TIMEOUT_S
    claude_bin: str = "claude"
    # "restricted": --restricted (no settings, no CLAUDE.md, file tools
    # confined to the clone). "project": --setting-sources project, which
    # loads the clone's CLAUDE.md; the user's ~/.claude/CLAUDE.md loads too,
    # but no user hooks or plugins. Every arm of a run shares the same mode.
    isolation: str = "restricted"
    # Interpreter of another gryphon checkout, for the graph_install_ref arm.
    ref_python: str | None = None


def gryphon_python(arm: str | None, settings: RunSettings) -> str:
    """The interpreter whose gryphon serves *arm*: the reference one for
    graph_install_ref, this one otherwise."""
    if arm in REF_ARMS:
        if not settings.ref_python:
            raise ValueError(f"arm {arm!r} needs --ref-python")
        return settings.ref_python
    return sys.executable


def _ref_call(python: str, code: str, *args: str) -> str:
    """Run *code* with the reference interpreter's gryphon and return stdout.

    ``-I`` keeps the current directory off ``sys.path``: run from this
    checkout, ``python -c`` would import this gryphon instead of the
    reference one.
    """
    import tempfile

    return subprocess.run(
        [python, "-I", "-c", code, *args], capture_output=True, text=True, encoding="utf-8",
        check=True, timeout=120, stdin=subprocess.DEVNULL, cwd=tempfile.gettempdir(),
    ).stdout


def ref_commit(python: str) -> str:
    """The git commit of the checkout the reference gryphon is imported from."""
    folder = _ref_call(
        python, "import gryphon, os; print(os.path.dirname(gryphon.__file__))",
    ).strip()
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=folder, capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def mcp_config(sandbox: ArmSandbox, python: str | None = None) -> dict:
    """MCP servers for the arm: none for baseline, gryphon for graph."""
    if sandbox.kind != "graph":
        return {"mcpServers": {}}
    return {
        "mcpServers": {
            "gryphon": {
                "command": python or sys.executable,
                "args": ["-m", "gryphon", "serve", "--repo", str(sandbox.repo)],
                "env": sandbox.gryphon_env(),
            }
        }
    }


def _fwd(path: Path | str) -> str:
    return str(path).replace("\\", "/")


def graph_settings(sandbox: ArmSandbox, *, enrich: bool = False) -> dict:
    """The SessionStart hook ``gryphon install`` adds for Claude Code.

    The installed hook calls ``gryphon`` from PATH; here it calls the running
    interpreter so it works from a project venv. The PostToolUse update hook
    is left out: reviewers do not edit files. *enrich* adds the
    ``gryphon enrich`` PreToolUse hook, which the install does not set up.
    """
    py = f'"{_fwd(sys.executable)}" -m gryphon'
    hooks: dict = {
        "SessionStart": [{
            "matcher": "",
            "hooks": [{
                "type": "command",
                "command": f'{py} status --repo "{_fwd(sandbox.repo)}"',
                "timeout": 30,
            }],
        }],
    }
    if enrich:
        hooks["PreToolUse"] = [{
            "matcher": "Grep|Glob|Bash|Read",
            "hooks": [{"type": "command", "command": f"{py} enrich", "timeout": 30}],
        }]
    return {"hooks": hooks}


def graph_instructions() -> str:
    """The CLAUDE.md block ``gryphon install`` writes, verbatim."""
    from gryphon.skills import _CLAUDE_MD_SECTION

    return _CLAUDE_MD_SECTION


def build_command(
    sandbox: ArmSandbox,
    settings: RunSettings,
    *,
    arm: str | None = None,
    schema: dict | None = None,
) -> list[str]:
    """The ``claude -p`` argv for one arm. The prompt goes through stdin.

    The graph arm reproduces what ``gryphon install`` gives Claude Code: the
    MCP server, the SessionStart status hook and the CLAUDE.md instruction
    block (passed as an appended system prompt, since the clone must stay
    identical between arms). *schema* overrides the findings schema (the
    judge runs with the same isolation and its own output contract).
    """
    arm = arm or sandbox.kind
    python = gryphon_python(arm, settings)
    if settings.isolation == "restricted":
        # Ignores user, project and local settings (so no hooks run) and
        # confines the file tools to the clone. Does not load CLAUDE.md.
        isolation = ["--restricted"]
    elif settings.isolation == "project":
        # The clone has no .claude/ (sparse checkout), so this loads no
        # settings and no hooks, but does load the clone's CLAUDE.md.
        isolation = ["--setting-sources", "project"]
    else:
        raise ValueError(f"unknown isolation {settings.isolation!r}")
    cmd = [
        settings.claude_bin, "-p", *isolation, "--tools", BUILTIN_TOOLS,
        "--strict-mcp-config", "--mcp-config", json.dumps(mcp_config(sandbox, python)),
        "--disable-slash-commands", "--no-session-persistence",
        "--model", settings.model, "--effort", settings.effort,
        "--max-budget-usd", str(settings.max_budget_usd),
        "--output-format", "stream-json", "--verbose", "--include-hook-events",
        "--json-schema", json.dumps(schema or FINDINGS_SCHEMA),
    ]
    allowed = list(READ_ONLY_BASH)
    if sandbox.kind == "graph":
        allowed.append(f"{GRAPH_TOOL_PREFIX}*")
        if arm == "graph_install":
            from gryphon.skills import generate_hooks_config

            hooks = generate_hooks_config(sandbox.repo)
        elif arm in REF_ARMS:
            hooks = json.loads(_ref_call(
                python,
                "import json, sys; from pathlib import Path; "
                "from gryphon.skills import generate_hooks_config; "
                "print(json.dumps(generate_hooks_config(Path(sys.argv[1]))))",
                str(sandbox.repo),
            ))
        else:
            hooks = graph_settings(sandbox, enrich=arm == "graph_md_enrich")
        cmd += ["--settings", json.dumps(hooks)]
        if arm not in CLAUDE_MD_ARMS:
            cmd += ["--append-system-prompt", graph_instructions()]
    return cmd + ["--disallowedTools", *DENIED_TOOLS, "--allowedTools", *allowed]


def parse_stream(lines) -> dict:
    """Pull init info, tool calls and the result event out of stream-json."""
    init: dict = {}
    result: dict | None = None
    tool_calls: list[dict] = []
    hook_events: list[str] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            init = event
        elif kind == "system" and "hook" in str(event.get("subtype", "")):
            hook_events.append(
                f"{event.get('subtype')}:{event.get('hook_event') or event.get('hook_name') or ''}"
            )
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_calls.append({"name": block.get("name"), "input": block.get("input")})
        elif kind == "result":
            result = event
    return {"init": init, "result": result, "tool_calls": tool_calls, "hook_events": hook_events}


_NETWORK_RE = re.compile(
    r"(?<![\w-])(gh|curl|wget|iwr|Invoke-WebRequest|Invoke-RestMethod)(?![\w-])"
    r"|git\s+(fetch|pull|push|clone|remote|ls-remote)\b",
    re.I,
)
# A drive letter must not follow another word character: in `grep "user_id:\|x"`
# the `d:\` is part of a regex, not the D: drive.
_ABS_PATH_RE = re.compile(
    r"(?:(?<![\w])[A-Za-z]:[\\/]|(?<![\w.])/[a-zA-Z]/|(?<![\w])~[\\/])[^\s\"'`;|&<>()]*"
)
_PARENT_REL_RE = re.compile(r"(?<![\w.])\.\.[\\/]")


def _norm(path: str) -> str:
    p = path.replace("\\", "/").rstrip("/").lower()
    m = re.match(r"^/([a-z])/(.*)$", p)
    if m:
        p = f"{m.group(1)}:/{m.group(2)}"
    return p


def _outside(path: str, roots: tuple[str, ...]) -> bool:
    p = _norm(path)
    return not any(p == r or p.startswith(r + "/") for r in roots)


def _allowed_roots(sandbox: ArmSandbox) -> tuple[str, ...]:
    # Claude Code persists large tool outputs under ~/.claude/projects and the
    # reviewer reads them back; that is its own transcript, not a leak.
    return (_norm(str(sandbox.repo)), _norm(str(Path.home() / ".claude" / "projects")))


def audit_tool_calls(
    tool_calls: list[dict], sandbox: ArmSandbox, source_repo: str,
) -> tuple[list[str], list[str]]:
    """``(leaks, notes)`` for tool calls that could have reached the PR's future.

    Leaks disqualify a run from blind scoring: network commands, the source
    repository, absolute paths outside the clone. Notes need a human look but
    are usually benign: a parent-relative path is fine after ``cd subdir``
    and only escapes the clone from its root.
    """
    roots = _allowed_roots(sandbox)
    source = _norm(source_repo)
    leaks: list[str] = []
    notes: list[str] = []
    for i, call in enumerate(tool_calls):
        name = call.get("name") or ""
        args = call.get("input") or {}
        if name == "Bash":
            cmd = str(args.get("command", ""))
            if _NETWORK_RE.search(cmd):
                leaks.append(f"#{i} network command: {cmd[:160]}")
            if source in _norm(cmd):
                leaks.append(f"#{i} references the source repo: {cmd[:160]}")
            for m in _ABS_PATH_RE.finditer(cmd):
                if _outside(m.group(0), roots):
                    leaks.append(f"#{i} absolute path outside the clone: {m.group(0)[:160]}")
            if _PARENT_REL_RE.search(cmd):
                notes.append(f"#{i} parent-relative path: {cmd[:160]}")
        elif name in ("Read", "Grep", "Glob"):
            for key in ("file_path", "path"):
                value = args.get(key)
                if value and re.match(r"^([A-Za-z]:[\\/]|/)", str(value)) and _outside(
                    str(value), roots
                ):
                    leaks.append(f"#{i} {name} outside the clone: {value}")
    return leaks, notes


def _install_claude_md(sandbox: ArmSandbox, python: str | None = None):
    """Write the install's CLAUDE.md block into the clone, the way
    ``gryphon install`` does, and return a callable that undoes it.

    The graph clone is shared with arms that must not see the block, so the
    file goes back to its tracked content (or away) right after the run.
    *python* writes the block of another gryphon checkout instead.
    """
    from gryphon.skills import inject_claude_md

    path = sandbox.repo / "CLAUDE.md"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "CLAUDE.md"],
        cwd=str(sandbox.repo), capture_output=True,
    ).returncode == 0
    if python and python != sys.executable:
        _ref_call(
            python,
            "import sys; from pathlib import Path; "
            "from gryphon.skills import inject_claude_md; inject_claude_md(Path(sys.argv[1]))",
            str(sandbox.repo),
        )
    else:
        inject_claude_md(sandbox.repo)

    def restore() -> None:
        if tracked:
            subprocess.run(
                ["git", "checkout", "--", "CLAUDE.md"],
                cwd=str(sandbox.repo), capture_output=True, check=True,
            )
        else:
            path.unlink(missing_ok=True)

    return restore


def _run_claude(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kwargs)


def _child_env(sandbox: ArmSandbox, python: str | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CRG_")}
    for var in _PARENT_SESSION_VARS:
        env.pop(var, None)
    # The SessionStart hook runs gryphon in the claude process environment.
    env.update(sandbox.gryphon_env())
    if sandbox.kind == "graph":
        # Installed hooks call `gryphon` from PATH, as on a user's machine.
        scripts = str(Path(python or sys.executable).parent)
        env["PATH"] = scripts + os.pathsep + env.get("PATH", "")
    return env


def summarize(parsed: dict, *, exit_code: int | None, timed_out: bool) -> dict:
    """Flatten a parsed stream into the record fields."""
    result = parsed["result"] or {}
    usage = result.get("usage") or {}
    structured = result.get("structured_output")
    calls = Counter(c["name"] for c in parsed["tool_calls"])
    if timed_out:
        status = "timeout"
    elif not result:
        status = "no_result"
    elif result.get("api_error_status"):
        status = f"api_error:{result.get('api_error_status')}"
    elif result.get("is_error") or result.get("subtype") != "success":
        status = f"error:{result.get('terminal_reason') or result.get('subtype')}"
    elif not isinstance(structured, dict):
        status = "no_structured_output"
    else:
        status = "ok"
    return {
        "status": status,
        "exit_code": exit_code,
        "num_turns": result.get("num_turns"),
        "duration_ms": result.get("duration_ms"),
        "duration_api_ms": result.get("duration_api_ms"),
        "total_cost_usd": result.get("total_cost_usd"),
        "input_tokens": usage.get("input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "tool_calls_total": sum(calls.values()),
        "tool_calls": dict(calls),
        "graph_tool_calls": sum(n for k, n in calls.items() if k.startswith(GRAPH_TOOL_PREFIX)),
        "permission_denials": len(result.get("permission_denials") or []),
        "hook_events": parsed["hook_events"],
        "api_error_status": result.get("api_error_status"),
        "result_text": (
            str(result.get("result"))[:200] if result.get("api_error_status") else None
        ),
        "init_tools": parsed["init"].get("tools"),
        "mcp_servers": parsed["init"].get("mcp_servers"),
        "summary": structured.get("summary") if isinstance(structured, dict) else None,
        "findings": structured.get("findings") if isinstance(structured, dict) else None,
    }


def run_arm(
    case: ReviewCase,
    sandbox: ArmSandbox,
    settings: RunSettings,
    *,
    arm: str,
    rep: int,
    run_dir: Path,
) -> dict:
    """Run one review and return its record. The transcript lands in *run_dir*."""
    if sandbox_kind(arm) != sandbox.kind:
        raise ValueError(f"arm {arm!r} cannot run in a {sandbox.kind!r} sandbox")
    if arm in CLAUDE_MD_ARMS and settings.isolation != "project":
        raise ValueError(
            f"arm {arm!r} needs isolation='project': --restricted skips CLAUDE.md"
        )
    files = changed_files(sandbox)
    prompt = build_prompt(arm, title=case.title, body=case.body, files=files)
    stem = f"{case.id}__{arm}__r{rep}"
    (run_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (run_dir / "streams").mkdir(parents=True, exist_ok=True)
    (run_dir / "prompts" / f"{case.id}__{arm}.txt").write_text(prompt, encoding="utf-8")
    stream_path = run_dir / "streams" / f"{stem}.jsonl"
    stderr_path = run_dir / "streams" / f"{stem}.stderr"

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started = time.monotonic()
    exit_code: int | None = None
    timed_out = False
    python = gryphon_python(arm, settings)
    restore = _install_claude_md(sandbox, python) if arm in CLAUDE_MD_ARMS else None
    with open(stream_path, "w", encoding="utf-8") as out, open(
        stderr_path, "w", encoding="utf-8"
    ) as err:
        try:
            proc = _run_claude(
                build_command(sandbox, settings, arm=arm),
                cwd=str(sandbox.repo), input=prompt, stdout=out, stderr=err,
                text=True, encoding="utf-8", errors="replace",
                env=_child_env(sandbox, python), timeout=settings.timeout_s,
            )
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            logger.warning("%s timed out after %ss", stem, settings.timeout_s)
        finally:
            if restore:
                restore()

    with open(stream_path, encoding="utf-8", errors="replace") as f:
        parsed = parse_stream(f)
    leaks, notes = audit_tool_calls(parsed["tool_calls"], sandbox, case.source_repo)
    record = {
        "case_id": case.id,
        "arm": arm,
        "rep": rep,
        "model": settings.model,
        "effort": settings.effort,
        "isolation": settings.isolation,
        "prompt_version": PROMPT_VERSION,
        "started_at": started_at,
        "wall_seconds": round(time.monotonic() - started, 1),
        "changed_files": len(files),
        "graph_build_seconds": sandbox.graph_build_seconds,
        "contamination_warnings": sandbox.contamination_warnings,
        "leak_flags": leaks,
        "audit_notes": notes,
        "stream": str(stream_path.relative_to(run_dir)),
        **summarize(parsed, exit_code=exit_code, timed_out=timed_out),
    }
    record["first_tools"] = [
        c["name"] for c in parsed["tool_calls"] if c["name"] != "StructuredOutput"
    ][:3]
    if arm == "graph_required":
        record["protocol_ok"] = record["first_tools"][:2] == list(REQUIRED_FIRST_CALLS)
    return record


def reaudit_run(run_dir: Path, cases: list[ReviewCase], workdir: Path) -> int:
    """Recompute leak flags and notes of every record from its saved stream.

    Lets an audit fix apply to runs already made, without re-running reviews.
    Returns how many records changed.
    """
    by_id = {c.id: c for c in cases}
    records_path = run_dir / "records.jsonl"
    records = [
        json.loads(line)
        for line in records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    changed = 0
    for rec in records:
        case = by_id.get(rec["case_id"])
        if case is None:
            continue
        kind = sandbox_kind(rec["arm"])
        root = workdir / _opaque_name(case.id, kind)
        sandbox = ArmSandbox(case_id=case.id, kind=kind, root=root, repo=root / "repo")
        with open(run_dir / rec["stream"], encoding="utf-8", errors="replace") as f:
            parsed = parse_stream(f)
        leaks, notes = audit_tool_calls(parsed["tool_calls"], sandbox, case.source_repo)
        if leaks != rec.get("leak_flags") or notes != rec.get("audit_notes"):
            rec["leak_flags"], rec["audit_notes"] = leaks, notes
            changed += 1
    records_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8",
    )
    return changed


STOP_STATUSES = frozenset({"api_error:429", "api_error:529"})


class RunAbortedError(RuntimeError):
    """The run stopped early because every later review would fail too."""


def run_cases(
    cases: list[ReviewCase],
    *,
    arms: tuple[str, ...] = ARMS,
    reps: int = 1,
    settings: RunSettings = RunSettings(),
    out_dir: Path,
    workdir: Path,
    fresh: bool = False,
    jobs: int = 1,
) -> Path:
    """Run every case × arm × rep and append records to ``records.jsonl``.

    Arms alternate order between reps so neither always runs first (rate
    limits and cache warmth drift over a long run). *jobs* cases run at once;
    the arms of one case stay sequential, since the graph arms share a clone
    and write CLAUDE.md into it. Returns the run directory.
    """
    for arm in arms:
        if arm not in ARMS:
            raise ValueError(f"unknown arm {arm!r}; expected one of {ARMS}")
    if any(a in REF_ARMS for a in arms) and not settings.ref_python:
        raise ValueError("graph_install_ref needs --ref-python")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = out_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    records_path = run_dir / "records.jsonl"
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": run_id,
        "cases": [c.id for c in cases],
        "arms": list(arms),
        "reps": reps,
        "model": settings.model,
        "effort": settings.effort,
        "isolation": settings.isolation,
        "max_budget_usd": settings.max_budget_usd,
        "prompt_version": PROMPT_VERSION,
        "gryphon_commit": ref_commit(sys.executable),
        "ref_python": settings.ref_python,
        "ref_commit": ref_commit(settings.ref_python) if settings.ref_python else None,
    }, indent=2), encoding="utf-8")

    lock = threading.Lock()
    stop = threading.Event()
    aborted: list[str] = []

    def run_case(case: ReviewCase) -> None:
        if stop.is_set():
            return
        # One sandbox per kind: the graph arms share a clone and a graph.
        by_kind: dict[str, ArmSandbox] = {}
        try:
            for arm in arms:
                kind = sandbox_kind(arm)
                if kind not in by_kind:
                    by_kind[kind] = prepare_arm(case, arm, workdir, fresh=fresh)
        except NoMergeBaseError as exc:
            # One case the clone cannot serve must not cost the whole run.
            logger.warning("skipping %s", exc)
            print(f"{case.id}: skipped ({exc})")
            return
        for rep in range(reps):
            order = list(arms) if rep % 2 == 0 else list(reversed(arms))
            for arm in order:
                if stop.is_set():
                    return
                logger.info("running %s / %s / rep %d", case.id, arm, rep)
                record = run_arm(
                    case, by_kind[sandbox_kind(arm)], settings,
                    arm=arm, rep=rep, run_dir=run_dir,
                )
                with lock:
                    with open(records_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    print(
                        f"{case.id} {arm} r{rep}: {record['status']} "
                        f"${record['total_cost_usd']} turns={record['num_turns']} "
                        f"findings={len(record['findings'] or [])} "
                        f"graph_calls={record['graph_tool_calls']} "
                        f"leaks={len(record['leak_flags'])}",
                        flush=True,
                    )
                if record["status"] in STOP_STATUSES:
                    # A usage or rate limit fails every later run the same way.
                    aborted.append(
                        f"{case.id}/{arm}/r{rep} hit {record['status']}"
                        f" ({record.get('result_text') or 'no message'})"
                    )
                    stop.set()
                    return

    if jobs <= 1:
        for case in cases:
            run_case(case)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for future in [pool.submit(run_case, c) for c in cases]:
                future.result()
    if aborted:
        raise RunAbortedError(
            f"{aborted[0]}; run stopped, records so far in {records_path}"
        )
    return run_dir

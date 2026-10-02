"""Graph enrichment for Claude Code hooks.

``PreToolUse`` (Grep, Glob, Read, Bash): for a file read, the callers and
tests of the symbols read; for a search, the same for the symbols named
exactly by the search term. Reads done through Bash (``sed -n A,Bp``,
``head``, ``cat``) count as reads, limited to the lines read.

``PostToolUse`` (Bash): after a ``git diff``, the changed symbols called from
outside the diff and the ones with no direct test (see ``diff_context``).

Context already given in a session is not repeated.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from .diff_context import VENDORED_RE

logger = logging.getLogger(__name__)

# Flags that consume the next token in grep/rg commands
_RG_FLAGS_WITH_VALUES = frozenset({
    "-e", "-f", "-m", "-A", "-B", "-C", "-g", "--glob",
    "-t", "--type", "--include", "--exclude", "--max-count",
    "--max-depth", "--max-filesize", "--color", "--colors",
    "--context-separator", "--field-match-separator",
    "--path-separator", "--replace", "--sort", "--sortr",
})


def extract_pattern(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """Extract a search pattern from a tool call's input.

    Returns None if no meaningful pattern can be extracted.
    """
    if tool_name == "Grep":
        return tool_input.get("pattern")

    if tool_name == "Glob":
        raw = tool_input.get("pattern", "")
        # Extract meaningful name from glob: "**/auth*.ts" -> "auth"
        # Skip pure extension globs like "**/*.ts"
        match = re.search(r"[*/]([a-zA-Z][a-zA-Z0-9_]{2,})", raw)
        return match.group(1) if match else None

    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if not re.search(r"\brg\b|\bgrep\b", cmd):
            return None
        pattern = _bash_grep_pattern(cmd)
        return pattern if pattern and len(pattern) >= 3 else None

    return None


def _bash_grep_pattern(cmd: str) -> str | None:
    """The pattern of the first grep/rg/git grep in *cmd*, quotes respected."""
    from .diff_context import _segments

    for seg in _segments(cmd):
        names = [Path(t).name for t in seg[:2]]
        if names[:1] in (["grep"], ["rg"], ["egrep"]):
            rest = seg[1:]
        elif names == ["git", "grep"]:
            rest = seg[2:]
        else:
            continue
        skip_next = False
        for i, token in enumerate(rest):
            if skip_next:
                skip_next = False
                continue
            if token in ("-e", "--regexp") and i + 1 < len(rest):
                return rest[i + 1]
            if token.startswith("-"):
                if token in _RG_FLAGS_WITH_VALUES:
                    skip_next = True
                continue
            return token
    return None


# Words that start a definition or a statement: searching the graph for them
# returns noise (``grep "def foo"`` is a search for ``foo``).
_KEYWORDS = frozenset({
    "def", "class", "function", "const", "let", "var", "async", "await",
    "export", "import", "from", "return", "public", "private", "protected",
    "static", "interface", "type", "struct", "enum", "func", "self", "this",
    "none", "true", "false", "null", "new", "with", "for", "while", "else",
    "elif", "raise", "throw", "yield", "lambda", "pass", "and", "not",
})
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def extract_search_terms(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    """Up to three identifiers worth looking up for a search call.

    Splits regex alternation (``a\\|b``, ``a|b``) and takes the first
    non-keyword identifier of each alternative.
    """
    raw = extract_pattern(tool_name, tool_input)
    if not raw:
        return []
    if tool_name == "Glob":
        return [raw]
    terms: list[str] = []
    for alternative in re.split(r"\\\||\|", raw):
        for ident in _IDENT_RE.findall(alternative):
            if ident.lower() in _KEYWORDS:
                continue
            if ident not in terms:
                terms.append(ident)
            break
    return terms[:3]


_SED_RANGE_RE = re.compile(r"^(\d+)(?:,(\d+))?p$")


def _under(directory: str, path: str) -> str:
    """*path* as seen from the start when the shell was in *directory*."""
    if not directory or re.match(r"^([A-Za-z]:[\\/]|/)", path):
        return path
    return f"{directory.rstrip('/')}/{path}"


def extract_file_reads(
    tool_name: str, tool_input: dict[str, Any],
) -> list[tuple[str, int | None, int | None]]:
    """``(path, start, end)`` for file reads, including those done in Bash.

    ``None`` bounds mean the whole file. Handles the Read tool (offset and
    limit), ``sed -n A,Bp``, ``head -n N`` / ``head -N`` and ``cat``.
    """
    if tool_name == "Read":
        path = tool_input.get("file_path")
        if not path:
            return []
        offset = tool_input.get("offset")
        limit = tool_input.get("limit")
        start = int(offset) if isinstance(offset, int) and offset > 0 else None
        end = (start or 1) + int(limit) - 1 if isinstance(limit, int) and limit > 0 else None
        return [(path, start, end)]
    if tool_name != "Bash":
        return []
    from .diff_context import segments_with_dir

    reads: list[tuple[str, int | None, int | None]] = []
    for directory, seg in segments_with_dir(tool_input.get("command", "")):
        name = Path(seg[0]).name
        args = seg[1:]
        files: list[str] = []
        span: tuple[int | None, int | None] = (None, None)
        if name == "sed":
            script = next((a for a in args if _SED_RANGE_RE.match(a)), None)
            if "-n" not in args or script is None:
                continue
            m = _SED_RANGE_RE.match(script)
            assert m is not None
            span = (int(m.group(1)), int(m.group(2) or m.group(1)))
            files = [a for a in args if a != script and not a.startswith("-")]
        elif name == "head":
            count = 10
            skip = False
            for i, a in enumerate(args):
                if skip:
                    skip = False
                    continue
                if a == "-n" and i + 1 < len(args) and args[i + 1].isdigit():
                    count, skip = int(args[i + 1]), True
                elif re.fullmatch(r"-\d+", a):
                    count = int(a[1:])
                elif not a.startswith("-"):
                    files.append(a)
            span = (1, count)
        elif name == "cat":
            files = [a for a in args if not a.startswith("-")]
        # After `cd x &&` a relative path is relative to x; keep it relative
        # to the starting directory so the repo root still resolves it.
        reads += [(_under(directory, f), *span) for f in files]
    return reads


def _make_relative(file_path: str, repo_root: str) -> str:
    """Make a file path relative to repo_root for display."""
    try:
        return str(Path(file_path).relative_to(repo_root))
    except ValueError:
        return file_path


def _format_node_context(node: Any, store: Any, repo_root: str) -> list[str]:
    """What the code being read cannot show about *node*: who calls it, its tests.

    Callees, flows and communities are left out: the callees are in the code
    the agent is reading, and the rest did not help a review.
    """
    from .diff_context import (
        NO_STATIC_CALLERS,
        _callers,
        caller_label,
        dynamic_entry,
        sibling_receivers,
    )

    loc = _make_relative(node.file_path, repo_root)
    if node.line_start:
        loc = f"{loc}:{node.line_start}"
    lines = [f"{node.name} ({loc})"]

    # Callers (max 5, deduplicated), with the same edge rules as the diff
    # context: exact edges, plus name-only edges when the name is unique.
    found = _callers(store, node)
    callers: list[str] = []
    seen: set[str] = set()
    for c in found:
        if len(callers) >= 5:
            break
        if c.node.name not in seen and not VENDORED_RE.search(c.node.file_path or ""):
            seen.add(c.node.name)
            callers.append(caller_label(c.node, c.signal, c.how))
    if callers:
        lines.append(f"  Called by: {', '.join(callers)}")
    elif node.kind in ("Function", "Method", "Class"):
        # Saying nothing read as "nothing calls this" and led reviewers to
        # call signal receivers and decorated hooks dead code.
        why = dynamic_entry(store, node)
        lines.append(f"  Called by: {NO_STATIC_CALLERS}" + (f"; {why}" if why else ""))
    for sender_name, others in sibling_receivers(store, node, found):
        names = ", ".join(f"{t.name} [{sig}]" for t, sig in others[:4])
        lines.append(f"  Other receivers of {sender_name}: {names}")

    # TESTED_BY edges are stored as source=production, target=test by the
    # parser, so look them up by source. See: #515
    tests: list[str] = []
    for e in store.get_edges_by_source(node.qualified_name):
        if e.kind == "TESTED_BY" and len(tests) < 3:
            t = store.get_node(e.target_qualified)
            if t and t.name not in tests:
                tests.append(t.name)
    if tests:
        lines.append(f"  Tests: {', '.join(tests)}")

    return lines


def _db_path(repo_root: str) -> Path:
    # Same resolution as the rest of gryphon (registry entry, then
    # CRG_DATA_DIR, then <repo>/.gryphon): a hard-coded .gryphon path made
    # the hook silently return nothing for graphs kept outside the repo.
    from .incremental import get_db_path

    return get_db_path(Path(repo_root), read_only=True)


# A name defined more often than this (``save``, ``get``) says nothing about
# which definition the search is after.
_MAX_DEFINITIONS = 3


def enrich_search(pattern: str, repo_root: str) -> str:
    """Context for the symbols named exactly *pattern*.

    A term that is not a symbol name (a log message, a config key, half a
    name) gets nothing. Keyword matching injected unrelated symbols there,
    and noise in the context costs a review more than a missing hint.
    """
    from .graph import GraphStore

    db_path = _db_path(repo_root)
    if not db_path.exists():
        return ""

    store = GraphStore(db_path)
    try:
        rows = store._conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND is_test = 0 "
            "AND kind IN ('Function', 'Method', 'Class', 'Type') LIMIT 20",
            (pattern,),
        ).fetchall()
        nodes = [
            n for n in (store.get_node_by_id(r[0]) for r in rows)
            if n is not None and not VENDORED_RE.search(n.file_path or "")
        ]
        if not nodes or len(nodes) > _MAX_DEFINITIONS:
            return ""
        all_lines: list[str] = []
        for node in nodes:
            all_lines.extend(_format_node_context(node, store, repo_root))
            all_lines.append("")
        header = f'[gryphon] {len(nodes)} symbol(s) named "{pattern}":\n'
        return header + "\n".join(all_lines)
    finally:
        store.close()


def enrich_file_read(
    file_path: str, repo_root: str, *, seen: "_SessionMemory | None" = None,
) -> str:
    """Context for the functions and classes of a file read whole."""
    from .graph import GraphStore

    db_path = _db_path(repo_root)
    if not db_path.exists():
        return ""

    store = GraphStore(db_path)
    try:
        nodes = store.get_nodes_by_file(file_path)
        if not nodes:
            # Try with resolved path
            try:
                resolved = str(Path(file_path).resolve())
                nodes = store.get_nodes_by_file(resolved)
            except (OSError, ValueError):
                pass
        interesting = [
            n for n in nodes
            if n.kind in ("Function", "Method", "Class", "Type") and not n.is_test
        ]
        if seen is not None:
            fresh = seen.filter([n.qualified_name for n in interesting[:8]])
            interesting = [n for n in interesting if n.qualified_name in fresh]
        interesting = interesting[:8]
        if not interesting:
            return ""

        all_lines: list[str] = []
        for node in interesting:
            all_lines.extend(_format_node_context(node, store, repo_root))
            all_lines.append("")

        rel_path = _make_relative(file_path, repo_root)
        return f"[gryphon] {len(interesting)} symbol(s) in {rel_path}:\n" + "\n".join(all_lines)
    finally:
        store.close()


def enrich_file_range(
    file_path: str, repo_root: str, start: int | None, end: int | None,
    *, seen: "_SessionMemory | None" = None,
) -> str:
    """Context for the symbols of *file_path* that overlap ``start..end``.

    Whole-file reads (no bounds) fall back to :func:`enrich_file_read`.
    """
    if start is None and end is None:
        return enrich_file_read(_absolute(file_path, repo_root), repo_root, seen=seen)
    from .graph import GraphStore

    db_path = _db_path(repo_root)
    if not db_path.exists():
        return ""
    lo, hi = start or 1, end or 10**9
    store = GraphStore(db_path)
    try:
        path = _absolute(file_path, repo_root)
        nodes = store.get_nodes_by_file(path) or store.get_nodes_by_file(
            Path(path).as_posix()
        )
        picked = [
            n for n in nodes
            if n.kind in ("Function", "Class", "Type", "Method")
            and not n.is_test
            and n.line_start is not None and n.line_end is not None
            and n.line_start <= hi and n.line_end >= lo
        ]
        if seen is not None:
            fresh = seen.filter([n.qualified_name for n in picked])
            picked = [n for n in picked if n.qualified_name in fresh]
        picked = picked[:5]
        if not picked:
            return ""
        lines = [f"[gryphon] {len(picked)} symbol(s) in {_make_relative(path, repo_root)}"
                 f":{lo}-{hi if end else 'end'}:"]
        for node in picked:
            lines.extend(_format_node_context(node, store, repo_root))
            lines.append("")
        return "\n".join(lines)
    finally:
        store.close()


def _absolute(file_path: str, repo_root: str) -> str:
    p = Path(file_path)
    return str(p if p.is_absolute() else Path(repo_root) / p)


class _SessionMemory:
    """Keys of context already injected in one session, kept in the data dir.

    Best-effort: an unwritable data dir only means context may repeat.
    """

    _MAX_KEYS = 500
    _MAX_FILES = 50

    def __init__(self, repo_root: str, session_id: str | None):
        self.path: Path | None = None
        self.keys: list[str] = []
        safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "")[:64]
        if not safe:
            return
        from .incremental import get_data_dir

        try:
            folder = get_data_dir(Path(repo_root), create=False) / "hook-sessions"
            self.path = folder / f"{safe}.json"
            if self.path.exists():
                self.keys = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("enrich: session memory unavailable: %s", exc)
            self.path = None

    def filter(self, keys: list[str]) -> set[str]:
        """The keys not given before; they are remembered from now on."""
        fresh = [k for k in keys if k not in self.keys]
        if fresh:
            self.keys = (self.keys + fresh)[-self._MAX_KEYS:]
            self._save()
        return set(fresh)

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.keys), encoding="utf-8")
            files = sorted(self.path.parent.glob("*.json"), key=lambda p: p.stat().st_mtime)
            for old in files[:-self._MAX_FILES]:
                old.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("enrich: could not save session memory: %s", exc)


def build_context(hook_input: dict[str, Any], repo_root: str) -> tuple[str, str]:
    """``(event, context)`` for one hook call; empty context means nothing to add."""
    from .diff_context import build_diff_context, extract_git_diffs, join_dir

    event = hook_input.get("hook_event_name") or "PreToolUse"
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input") or {}
    seen = _SessionMemory(repo_root, hook_input.get("session_id"))
    parts: list[str] = []

    if event == "PostToolUse":
        if tool_name == "Bash":
            cwd = hook_input.get("cwd") or repo_root
            for directory, args in extract_git_diffs(tool_input.get("command", ""))[:2]:
                git_cwd = join_dir(cwd, directory) if directory else cwd
                if seen.filter([f"diff:{git_cwd}\x00" + "\x00".join(args)]):
                    text = build_diff_context(repo_root, args, cwd=git_cwd)
                    if text:
                        parts.append(text)
        return event, "\n\n".join(parts)

    reads = extract_file_reads(tool_name, tool_input)
    if reads:
        for path, start, end in reads[:3]:
            text = enrich_file_range(path, repo_root, start, end, seen=seen)
            if text:
                parts.append(text)
        return event, "\n\n".join(parts)

    terms = extract_search_terms(tool_name, tool_input)
    fresh_terms = seen.filter([f"term:{t}" for t in terms]) if terms else set()
    for term in terms:
        if f"term:{term}" in fresh_terms:
            text = enrich_search(term, repo_root)
            if text:
                parts.append(text)
    return event, "\n\n".join(parts)


def run_hook(repo: str | None = None) -> None:
    """Entry point for the enrich CLI subcommand.

    Reads Claude Code hook JSON from stdin and writes ``hookSpecificOutput``
    JSON to stdout when there is graph context to add. *repo* overrides the
    repository root found from the hook's ``cwd``.
    """
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    if not isinstance(hook_input, dict):
        return
    cwd = hook_input.get("cwd", os.getcwd())

    from .incremental import find_project_root, get_db_path

    repo_path = Path(repo) if repo else find_project_root(Path(cwd))
    if not get_db_path(repo_path, read_only=True).exists():
        return
    event, context = build_context(hook_input, str(repo_path))
    if not context:
        return

    response = {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": context,
        }
    }
    json.dump(response, sys.stdout)

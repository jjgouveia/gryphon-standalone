"""Graph context for a ``git diff`` the agent just ran.

A reviewer's first move is ``git diff``; what the diff cannot show is who
calls the changed code from outside it and which changed functions no test
reaches. This module recomputes the same diff with ``-U0``, maps the changed
lines to graph nodes and returns that context as a short block of text for a
Claude Code ``PostToolUse`` hook.

Only the revision and pathspec tokens of the agent's command are reused;
every option is dropped, so nothing like ``--output=<file>`` or an external
diff driver reaches git, and ``--no-ext-diff --no-textconv`` are forced.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "\n"}
# Options of `git` itself (before the subcommand) that take a value.
_GIT_GLOBAL_WITH_VALUE = {"-c", "--git-dir", "--work-tree", "--namespace"}
_SAFE_ARG = re.compile(r"^[\w./~^@{}:!*\-+=,]+$")
_GIT_TIMEOUT = 15

# Paths whose symbols are generated or vendored: never worth a reviewer's time.
VENDORED_RE = re.compile(
    r"(^|[\\/])(node_modules|staticfiles|static[\\/]dist|dist|build|vendor|\.venv|venv)"
    r"[\\/]|\.min\.(js|css)$|\.bundle\.js$",
    re.I,
)

# Test support that is not a Test node itself (conftest fixtures, helpers under
# tests/): a caller there is coverage, not production code that can break.
TEST_SUPPORT_RE = re.compile(
    r"(^|[\\/])(tests?|__tests__|spec)[\\/]|(^|[\\/])conftest\.py$"
    r"|(^|[\\/])test_[^\\/]+\.py$|[._-](test|spec)\.[jt]sx?$",
    re.I,
)

# What to say when the graph has no caller for a symbol. An empty answer reads
# as "nothing calls this"; in Django, signal receivers, decorated hooks and
# dynamic dispatch never show up as CALLS edges (a pilot review called a field
# set by a pre_save receiver dead code right after such an empty answer).
NO_STATIC_CALLERS = (
    "none found statically (signals, decorators and dynamic dispatch are not in "
    "the graph; grep for the name before calling it unused)"
)

MAX_NODES = 8
MAX_CALLERS = 4
MAX_UNTESTED = 10
MAX_CHARS = 3000


def _segments(command: str) -> list[list[str]]:
    """Shell-split *command* into simple commands (no expansion, no execution)."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:  # unbalanced quotes
        return []
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def segments_with_dir(command: str) -> list[tuple[str, list[str]]]:
    """Each simple command with the directory earlier ``cd``s moved to.

    ``""`` is the starting directory. ``cd`` alone, ``cd ~`` and ``cd -`` go
    back to ``""``: their target is unknown here, and a wrong guess would
    point reads at the wrong file. Agents write ``cd src/api && sed -n
    1,40p x.py``, so without this the read resolves against the wrong folder.
    """
    out: list[tuple[str, list[str]]] = []
    current = ""
    for seg in _segments(command):
        if seg[0] == "cd":
            target = seg[1] if len(seg) > 1 else ""
            if target in ("", "~", "-"):
                current = ""
            elif re.match(r"^([A-Za-z]:[\\/]|/)", target) or not current:
                current = target
            else:
                current = f"{current.rstrip('/')}/{target}"
            continue
        out.append((current, seg))
    return out


def join_dir(base: str, directory: str, path: str = "") -> str:
    """*path* resolved from *base* after ``cd directory``; an absolute piece wins."""
    result = Path(base)
    for piece in (directory, path):
        if piece:
            result = result / piece
    return str(result)


def extract_git_diffs(command: str) -> list[tuple[str, list[str]]]:
    """``(directory, args)`` for every ``git diff`` in *command*.

    *args* are the non-option arguments; ``--`` is kept when the agent used
    it, so pathspecs stay pathspecs. ``git diff --stat`` with no revisions
    yields ``[]`` (the working tree against the index), which is still a diff.
    *directory* is where earlier ``cd``s left the shell (see
    :func:`segments_with_dir`); pathspecs resolve from there.
    """
    found: list[tuple[str, list[str]]] = []
    for directory, seg in segments_with_dir(command):
        if not seg or Path(seg[0]).name not in {"git", "git.exe"}:
            continue
        i = 1
        while i < len(seg) and seg[i].startswith("-"):
            i += 2 if seg[i] in _GIT_GLOBAL_WITH_VALUE else 1
        if i >= len(seg) or seg[i] != "diff":
            continue
        args: list[str] = []
        after_dashdash = False
        for word in seg[i + 1:]:
            if word == "--":
                after_dashdash = True
                args.append(word)
                continue
            if not after_dashdash and word.startswith("-"):
                continue  # every option is dropped
            if not _SAFE_ARG.match(word):
                break  # redirection or something we will not pass to git
            args.append(word)
        found.append((directory, args))
    return found


def extract_git_diff_args(command: str) -> list[list[str]]:
    """The non-option arguments of every ``git diff`` in *command*."""
    return [args for _directory, args in extract_git_diffs(command)]


def diff_ranges(repo_root: str, args: list[str]) -> dict[str, list[tuple[int, int]]]:
    """Changed line ranges (new side) for ``git diff <args>``."""
    from .changes import _parse_unified_diff

    cmd = [
        "git", "diff", "--unified=0", "--no-color", "--no-ext-diff", "--no-textconv",
        *args,
    ]
    try:
        result = subprocess.run(
            cmd, cwd=repo_root, capture_output=True, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("diff context: git diff failed: %s", exc)
        return {}
    if result.returncode != 0:
        return {}
    return _parse_unified_diff(result.stdout)


def _norm(path: str) -> str:
    return path.replace("\\", "/").lower()


def _rel(path: str, repo_root: str) -> str:
    p, root = path.replace("\\", "/"), repo_root.replace("\\", "/").rstrip("/")
    if p.lower().startswith(root.lower() + "/"):
        return p[len(root) + 1:]
    return p


def _in_changed(file_path: str, changed: set[str]) -> bool:
    p = _norm(file_path)
    return any(p == c or p.endswith("/" + c) for c in changed)


def _callers(store, node) -> list:
    """CALLS sources of *node*: exact edges, then unqualified-name edges.

    Name-only edges are used only when the name is unique in the graph, the
    same rule ``query_graph_tool`` applies: for ``get`` or ``update`` they
    would match every ``.get()`` call in the repository.
    """
    seen: set[str] = set()
    callers = []
    edges = list(store.iter_edges_by_target(node.qualified_name))
    if store.count_nodes_by_name(node.name, language=node.language or None) == 1:
        edges += list(
            store.iter_edges_by_target_name(node.name, language=node.language or None)
        )
    for e in edges:
        if e.kind != "CALLS" or e.source_qualified in seen:
            continue
        if "ambiguous_targets" in (e.extra or {}):
            continue
        seen.add(e.source_qualified)
        caller = store.get_node(e.source_qualified)
        if caller is not None:
            signal = (e.extra or {}).get("django_signal")
            # A signal edge's line is the receiver's, in the receiver's file;
            # shown next to the sender it must be the sender's own line.
            callers.append((caller, caller.line_start if signal else e.line, signal))
    return callers


def caller_label(caller, signal: Optional[str]) -> str:
    """``Document (via pre_save)`` for a signal-wired caller, else the name."""
    return f"{caller.name} (via {signal})" if signal else caller.name


def sibling_receivers(store, node, callers: list) -> list[tuple[str, list]]:
    """Other receivers of the models that send signals to *node*.

    Receivers of one model share its instance across the save: a pre_save
    receiver can set what a post_save receiver reads, so a reviewer looking
    at one needs the others.
    """
    out = []
    for sender, _line, signal in callers:
        if not signal:
            continue
        others = []
        for e in store.iter_edges_by_source(sender.qualified_name):
            other_signal = (e.extra or {}).get("django_signal")
            if e.kind != "CALLS" or not other_signal or e.target_qualified == node.qualified_name:
                continue
            target = store.get_node(e.target_qualified)
            if target is not None:
                others.append((target, other_signal))
        if others:
            out.append((sender.name, others))
    return out


def _is_test_code(node) -> bool:
    return bool(node.is_test or TEST_SUPPORT_RE.search(node.file_path or ""))


def _has_test(store, node, callers: list) -> bool:
    """A TESTED_BY edge, or test code that calls the node directly."""
    if any(_is_test_code(c) for c, _line, _signal in callers):
        return True
    return any(e.kind == "TESTED_BY" for e in store.iter_edges_by_source(node.qualified_name))


def build_diff_context(
    repo_root: str, args: list[str], *, cwd: str | None = None, store=None,
) -> str:
    """The hook text for ``git diff <args>``: outside callers and untested changes.

    git runs in *cwd* (the agent's directory, where its pathspecs resolve);
    the paths it prints are relative to the repository root either way.
    """
    from .changes import map_changes_to_nodes

    ranges = diff_ranges(cwd or repo_root, args)
    ranges = {f: r for f, r in ranges.items() if not VENDORED_RE.search(f)}
    if not ranges:
        return ""
    own_store = store is None
    if own_store:
        from .graph import GraphStore
        from .incremental import get_db_path

        db_path = get_db_path(Path(repo_root), read_only=True)
        if not db_path.exists():
            return ""
        store = GraphStore(db_path)
    try:
        changed = {_norm(f) for f in ranges}
        nodes = [
            n for n in map_changes_to_nodes(store, ranges)
            if n.kind in ("Function", "Class", "Method") and not _is_test_code(n)
        ]
        if not nodes:
            return ""
        outside: list[tuple[Any, list]] = []
        untested = []
        siblings: list[tuple[Any, str, list]] = []
        for node in nodes:
            callers = _callers(store, node)
            # Tests calling the node are coverage, not callers that can break.
            ext = [
                (c, line, signal) for c, line, signal in callers
                if not _is_test_code(c)
                and not _in_changed(c.file_path, changed)
                and not VENDORED_RE.search(c.file_path)
            ]
            if ext:
                outside.append((node, ext))
            if not _has_test(store, node, callers):
                untested.append(node)
            for sender_name, others in sibling_receivers(store, node, callers):
                siblings.append((node, sender_name, others))
        outside.sort(key=lambda item: -len(item[1]))

        label = " ".join(a for a in args if a != "--") or "working tree"
        lines = [
            f"[gryphon] Graph context for `git diff {label}`: {len(nodes)} changed "
            f"function(s)/class(es) in {len(ranges)} file(s).",
        ]
        if outside:
            lines.append(
                "Called from OUTSIDE this diff (check these callers still hold):"
            )
            for node, ext in outside[:MAX_NODES]:
                where = f"{_rel(node.file_path, repo_root)}:{node.line_start}"
                shown = ", ".join(
                    f"{caller_label(c, signal)} ({_rel(c.file_path, repo_root)}:"
                    f"{line or c.line_start})"
                    for c, line, signal in ext[:MAX_CALLERS]
                )
                more = f" +{len(ext) - MAX_CALLERS} more" if len(ext) > MAX_CALLERS else ""
                lines.append(f"- {node.name} ({where}) <- {shown}{more}")
            if len(outside) > MAX_NODES:
                extra = len(outside) - MAX_NODES
                lines.append(f"  ({extra} more changed symbols have outside callers)")
        else:
            lines.append(
                "No callers outside this diff were found statically (signals, decorators "
                "and dynamic dispatch are not in the graph)."
            )
        if siblings:
            lines.append(
                "Changed signal receivers share their model with other receivers "
                "(one can set what another reads):"
            )
            for node, sender_name, others in siblings[:MAX_NODES]:
                shown = ", ".join(
                    f"{t.name} [{sig}] ({_rel(t.file_path, repo_root)}:{t.line_start})"
                    for t, sig in others[:MAX_CALLERS]
                )
                more = f" +{len(others) - MAX_CALLERS} more" if len(others) > MAX_CALLERS else ""
                lines.append(f"- {node.name} on {sender_name}: also {shown}{more}")
        if untested:
            names = ", ".join(n.name for n in untested[:MAX_UNTESTED])
            more = f" +{len(untested) - MAX_UNTESTED} more" if len(untested) > MAX_UNTESTED else ""
            lines.append(
                f"No direct test in the graph (may be covered indirectly) for: {names}{more}"
            )
        lines.append(
            "The graph can miss dynamic calls; verify in the source. "
            'More: query_graph_tool(pattern="callers_of"|"tests_for", target=<name>).'
        )
        text = "\n".join(lines)
        return text if len(text) <= MAX_CHARS else text[:MAX_CHARS - 3] + "..."
    finally:
        if own_store:
            store.close()

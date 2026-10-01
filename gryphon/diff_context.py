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
from typing import Any, NamedTuple, Optional

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
MAX_CHARS = 4000

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class Change(NamedTuple):
    """One added (``+``) or removed (``-``) line of a diff."""

    line: int
    sign: str
    text: str


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


def diff_changes(repo_root: str, args: list[str]) -> dict[str, list[Change]]:
    """Added and removed lines of ``git diff <args>``, per file (new-side path)."""
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
    return parse_diff_changes(result.stdout)


def parse_diff_changes(diff_text: str) -> dict[str, list[Change]]:
    """Added and removed lines of a ``--unified=0`` diff, per new-side path.

    A removed line has no new-side number; it is placed at the first new-side
    line of its hunk, which is where the removal shows in the new file. A
    deleted file (``+++ /dev/null``) is left out: none of its nodes remain.
    """
    changes: dict[str, list[Change]] = {}
    current: Optional[str] = None
    new_line = hunk_start = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            current = raw[6:] if raw.startswith("+++ b/") else None
            continue
        if raw.startswith("--- "):
            continue
        hunk = _HUNK_RE.match(raw)
        if hunk:
            hunk_start = new_line = int(hunk.group(1))
            if current is not None:
                changes.setdefault(current, [])
            continue
        if current is None or not raw or raw[0] not in "+-":
            continue
        if raw[0] == "+":
            changes[current].append(Change(new_line, "+", raw[1:]))
            new_line += 1
        else:
            changes[current].append(Change(hunk_start, "-", raw[1:]))
    return changes


def diff_ranges(repo_root: str, args: list[str]) -> dict[str, list[tuple[int, int]]]:
    """Changed lines (new side) of ``git diff <args>``, as one-line ranges."""
    return _ranges(diff_changes(repo_root, args))


def _ranges(changes: dict[str, list[Change]]) -> dict[str, list[tuple[int, int]]]:
    return {f: [(c.line, c.line) for c in cs] for f, cs in changes.items()}


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


def _file_changes(file_path: str, changes: dict[str, list[Change]]) -> list[Change]:
    p = _norm(file_path)
    for f, cs in changes.items():
        c = _norm(f)
        if p == c or p.endswith("/" + c):
            return cs
    return []


# A line that holds only a comment, a docstring delimiter or nothing changes
# no behaviour.
_INERT_RE = re.compile(r"^\s*($|#|//|/\*|\*|\"\"\"|''')")
_EXIT_RE = re.compile(r"^\s*(return|raise|throw|yield)\b")
_EXIT_TAGS = {"return": "return", "yield": "return", "raise": "raise", "throw": "raise"}
_DECL_PREFIX = (
    r"(?:(?:export|default|public|private|protected|static|async|override|readonly)\s+)*"
)


def _declares(text: str, name: str) -> bool:
    """Whether *text* is (part of) the declaration of *name*."""
    n = re.escape(name)
    return bool(
        re.search(rf"\b(def|class|function|fun|func|fn|interface)\s+{n}\b", text)
        or re.search(rf"\b{n}\s*[=:]\s*(async\s+)?(function\b|\([^)]*\)\s*(:[^=]+)?=>)", text)
        or re.match(rf"^\s*{_DECL_PREFIX}{n}\s*\(.*\)\s*(:\s*[^={{;]+)?\{{?\s*$", text)
    )


def contract_changes(node, changes: list[Change], inner: list[tuple[int, int]]) -> list[str]:
    """What the diff changed in *node*'s contract with its callers.

    ``signature`` (the declaration), ``return`` / ``raise`` (an exit of the
    body), ``fields`` (a class body line outside its methods). An empty list
    means the change stays inside the body. *inner* are the spans of nested
    functions and methods, whose exits are their own.

    Line based: a parameter on a continuation line of a multi-line signature
    reads as a body change.
    """
    lo, hi = node.line_start, node.line_end
    tags: list[str] = []

    def add(tag: str) -> None:
        if tag not in tags:
            tags.append(tag)

    for ch in changes:
        if not lo <= ch.line <= hi or _INERT_RE.match(ch.text):
            continue
        nested = any(a <= ch.line <= b for a, b in inner)
        if not nested and ((ch.line == lo and ch.sign == "+") or _declares(ch.text, node.name)):
            add("signature")
            continue
        if node.kind == "Class":
            if not nested and not ch.text.lstrip().startswith("@"):
                add("fields")
            continue
        exit_ = _EXIT_RE.match(ch.text)
        if exit_ and not nested:
            add(_EXIT_TAGS[exit_.group(1)])
    return tags


class Caller(NamedTuple):
    """One CALLS source of a node, as the diff and read contexts show it."""

    node: Any
    line: Optional[int]
    signal: Optional[str]
    # "" for an edge resolved to the node itself, "by name" for a name-only
    # edge (the name is unique, the call itself was not resolved), "inferred"
    # for an edge a post-build resolver added from a convention.
    how: str = ""


def _callers(store, node) -> list[Caller]:
    """CALLS sources of *node*: exact edges, then unqualified-name edges.

    Name-only edges are used only when the name is unique in the graph, the
    same rule ``query_graph_tool`` applies: for ``get`` or ``update`` they
    would match every ``.get()`` call in the repository.
    """
    seen: set[str] = set()
    callers: list[Caller] = []
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
        if caller is None:
            continue
        signal = (e.extra or {}).get("django_signal")
        if e.target_qualified != node.qualified_name:
            how = "by name"
        elif e.confidence_tier == "INFERRED" and not signal:
            how = "inferred"
        else:
            how = ""
        # A signal edge's line is the receiver's, in the receiver's file;
        # shown next to the sender it must be the sender's own line.
        callers.append(Caller(caller, caller.line_start if signal else e.line, signal, how))
    return callers


def caller_label(caller, signal: Optional[str], how: str = "") -> str:
    """``Document (via pre_save)`` for a signal-wired caller, ``run (by name)``
    for an unresolved one, else the name."""
    if signal:
        return f"{caller.name} (via {signal})"
    return f"{caller.name} ({how})" if how else caller.name


def sibling_receivers(store, node, callers: list[Caller]) -> list[tuple[str, list]]:
    """Other receivers of the models that send signals to *node*.

    Receivers of one model share its instance across the save: a pre_save
    receiver can set what a post_save receiver reads, so a reviewer looking
    at one needs the others.
    """
    out = []
    for c in callers:
        if not c.signal:
            continue
        others = []
        for e in store.iter_edges_by_source(c.node.qualified_name):
            other_signal = (e.extra or {}).get("django_signal")
            if e.kind != "CALLS" or not other_signal or e.target_qualified == node.qualified_name:
                continue
            target = store.get_node(e.target_qualified)
            if target is not None:
                others.append((target, other_signal))
        if others:
            out.append((c.node.name, others))
    return out


# Decorators that do not hand the function to a framework.
_PLAIN_DECORATORS = frozenset({
    "staticmethod", "classmethod", "property", "cached_property", "abstractmethod",
    "override", "wraps", "dataclass", "lru_cache", "cache", "total_ordering",
    "contextmanager", "asynccontextmanager",
})


def dynamic_entry(store, node) -> str:
    """Why *node* may be called with no CALLS edge to it, or ``""``.

    A decorator that registers it (``@receiver``, ``@app.route``,
    ``@shared_task``), a dunder method, or a method of a class that extends a
    base class: the framework or the base class calls it.
    """
    for deco in (node.extra or {}).get("decorators") or []:
        name = str(deco).lstrip("@").split("(", 1)[0].strip()
        if name and name.rsplit(".", 1)[-1] not in _PLAIN_DECORATORS:
            return f"decorated @{name}"
    if node.name.startswith("__") and node.name.endswith("__"):
        return "dunder method"
    if node.kind in ("Method", "Function") and node.parent_name:
        class_qn = f"{node.file_path}::{node.parent_name}"
        for e in store.iter_edges_by_source(class_qn):
            if e.kind in ("INHERITS", "IMPLEMENTS"):
                base = e.target_qualified.rsplit("::", 1)[-1]
                return f"method of {node.parent_name}({base})"
    return ""


def _is_test_code(node) -> bool:
    return bool(node.is_test or TEST_SUPPORT_RE.search(node.file_path or ""))


def _has_test(store, node, callers: list[Caller]) -> bool:
    """A TESTED_BY edge, or test code that calls the node directly."""
    if any(_is_test_code(c.node) for c in callers):
        return True
    return any(e.kind == "TESTED_BY" for e in store.iter_edges_by_source(node.qualified_name))


def _fan_in(store, qualified_name: str) -> int:
    row = store._conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind = 'CALLS' AND target_qualified = ?",
        (qualified_name,),
    ).fetchone()
    return int(row[0])


def _graph_commit_note(store, repo_root: str) -> str:
    """`` (graph at abc12345)``, or a warning when HEAD has moved since the build."""
    sha = store.get_metadata("git_head_sha") or ""
    if not sha:
        return ""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True,
            stdin=subprocess.DEVNULL, text=True, timeout=_GIT_TIMEOUT,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        head = ""
    if head and head != sha:
        return f" (graph built at {sha[:8]}, HEAD is {head[:8]}: line numbers may be off)"
    return f" (graph at {sha[:8]})"


class _Budget:
    """Lines in priority order; once one does not fit, the rest are only counted."""

    def __init__(self, limit: int):
        self.limit = limit
        self.lines: list[str] = []
        self.size = 0
        self.cut = 0

    def add(self, line: str) -> None:
        if self.cut or self.size + len(line) + 1 > self.limit:
            self.cut += 1
            return
        self.lines.append(line)
        self.size += len(line) + 1


FOOTER = (
    "Static graph: dynamic calls are not in it. Per symbol: "
    'query_graph_tool(pattern="callers_of"|"tests_for", target=<name>).'
)


def _function_spans(store, nodes: list) -> dict[str, list[tuple[str, int, int]]]:
    """``(qualified_name, start, end)`` of every function in the files of *nodes*."""
    out: dict[str, list[tuple[str, int, int]]] = {}
    for file_path in {n.file_path for n in nodes}:
        out[_norm(file_path)] = [
            (n.qualified_name, n.line_start, n.line_end)
            for n in store.get_nodes_by_file(file_path)
            if n.kind in ("Function", "Method") and n.line_start and n.line_end
        ]
    return out


def build_diff_context(
    repo_root: str, args: list[str], *, cwd: str | None = None, store=None,
) -> str:
    """The hook text for ``git diff <args>``: outside callers and untested changes.

    git runs in *cwd* (the agent's directory, where its pathspecs resolve);
    the paths it prints are relative to the repository root either way.

    Every changed symbol with callers outside the diff lists them, the most
    called first. Symbols whose signature, exits (``return``/``raise``) or
    class fields changed come first and carry those tags. A body-only change
    keeps its callers too: in the benchmark, the defect a hook-assisted review
    found and the others missed was a JSX change inside a ``return (...)``,
    seen through a caller this list named. The text stays under
    ``MAX_CHARS`` and says how much it cut.
    """
    from .changes import map_changes_to_nodes

    changes = diff_changes(cwd or repo_root, args)
    changes = {f: c for f, c in changes.items() if c and not VENDORED_RE.search(f)}
    if not changes:
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
        changed = {_norm(f) for f in changes}
        nodes = [
            n for n in map_changes_to_nodes(store, _ranges(changes))
            if n.kind in ("Function", "Class", "Method") and not _is_test_code(n)
        ]
        if not nodes:
            return ""
        spans = _function_spans(store, nodes)
        # A function nested in another one is part of it: its changes are the
        # outer function's, and no test can call it directly.
        nodes = [
            n for n in nodes
            if not any(
                qn != n.qualified_name and start <= n.line_start and n.line_end <= end
                and (start, end) != (n.line_start, n.line_end)
                for qn, start, end in spans.get(_norm(n.file_path), [])
                if n.kind == "Function"
            )
        ]
        fan_in: dict[str, int] = {}

        def rank(c: Caller) -> tuple:
            qn = c.node.qualified_name
            if qn not in fan_in:
                fan_in[qn] = _fan_in(store, qn)
            return (-fan_in[qn], bool(c.how), c.node.file_path or "", c.line or 0)

        outside: list[tuple[Any, list[str], list[Caller]]] = []
        unreached: list[tuple[Any, str]] = []
        untested = []
        siblings: list[tuple[Any, str, list]] = []
        for node in nodes:
            callers = _callers(store, node)
            # Tests calling the node are coverage, not callers that can break.
            ext = sorted(
                (
                    c for c in callers
                    if not _is_test_code(c.node)
                    and not _in_changed(c.node.file_path, changed)
                    and not VENDORED_RE.search(c.node.file_path)
                ),
                key=rank,
            )
            inner = [
                (start, end) for qn, start, end in spans.get(_norm(node.file_path), [])
                if qn != node.qualified_name
                and node.line_start <= start and end <= node.line_end
            ]
            tags = contract_changes(node, _file_changes(node.file_path, changes), inner)
            if ext:
                outside.append((node, tags, ext))
            elif all(_is_test_code(c.node) for c in callers):
                why = dynamic_entry(store, node)
                # Nobody takes a dunder method for dead code.
                if why and why != "dunder method":
                    unreached.append((node, why))
            if not _has_test(store, node, callers):
                untested.append(node)
            for sender_name, others in sibling_receivers(store, node, callers):
                siblings.append((node, sender_name, others))
        outside.sort(key=lambda item: (not item[1], -len(item[2])))

        def where(n) -> str:
            return f"{_rel(n.file_path, repo_root)}:{n.line_start}"

        def caller_list(ext: list[Caller]) -> str:
            shown = ", ".join(
                f"{caller_label(c.node, c.signal, c.how)} ({_rel(c.node.file_path, repo_root)}:"
                f"{c.line or c.node.line_start})"
                for c in ext[:MAX_CALLERS]
            )
            return shown + (f" +{len(ext) - MAX_CALLERS} more" if len(ext) > MAX_CALLERS else "")

        def more(items: list, limit: int) -> str:
            return f" +{len(items) - limit} more" if len(items) > limit else ""

        label = " ".join(a for a in args if a != "--") or "working tree"
        out = _Budget(MAX_CHARS - len(FOOTER) - 60)
        out.add(
            f"[gryphon] Graph context for `git diff {label}`"
            f"{_graph_commit_note(store, repo_root)}: {len(nodes)} changed "
            f"function(s)/class(es) in {len(changes)} file(s)."
        )
        if outside:
            out.add(
                "Called from OUTSIDE this diff ([tags]: what changed in the contract; "
                "most-called callers first):"
            )
            for node, tags, ext in outside[:MAX_NODES]:
                tag = f" [{', '.join(tags)}]" if tags else ""
                out.add(f"- {node.name} ({where(node)}){tag} <- {caller_list(ext)}")
            if len(outside) > MAX_NODES:
                rest = ", ".join(n.name for n, _tags, _ext in outside[MAX_NODES:MAX_NODES * 2])
                out.add(f"  (also with outside callers: {rest}"
                        f"{more(outside, MAX_NODES * 2)})")
        if siblings:
            out.add(
                "Changed signal receivers share their model with other receivers "
                "(one can set what another reads):"
            )
            for node, sender_name, others in siblings[:MAX_NODES]:
                shown = ", ".join(
                    f"{t.name} [{sig}] ({_rel(t.file_path, repo_root)}:{t.line_start})"
                    for t, sig in others[:MAX_CALLERS]
                )
                out.add(f"- {node.name} on {sender_name}: also {shown}"
                        f"{more(others, MAX_CALLERS)}")
        if not outside:
            out.add(
                "No callers outside this diff were found statically (signals, decorators "
                "and dynamic dispatch are not in the graph)."
            )
        if unreached:
            shown = ", ".join(f"{n.name} ({why})" for n, why in unreached[:MAX_NODES])
            out.add(f"No static caller, likely called by a framework or a base class: {shown}"
                    f"{more(unreached, MAX_NODES)}")
        if untested:
            names = ", ".join(n.name for n in untested[:MAX_UNTESTED])
            out.add(f"No direct test in the graph (may be covered indirectly) for: {names}"
                    f"{more(untested, MAX_UNTESTED)}")
        lines = out.lines
        if out.cut:
            lines.append(f"({out.cut} more line(s) cut to keep this short.)")
        lines.append(FOOTER)
        return "\n".join(lines)
    finally:
        if own_store:
            store.close()

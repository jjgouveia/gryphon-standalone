"""Resolve Django signal receivers to the models that send them.

``@receiver(pre_save, sender=Document)`` wires a function to every
``Document.save()`` at runtime; nothing calls it statically, so the graph
showed signal receivers with no caller at all. In an A/B review benchmark
that read as dead code: a reviewer called a field dead that a ``pre_save``
receiver of the same model sets.

This post-build pass reads the ``decorators`` the parser already stores on
Python Function nodes, and for every ``receiver(...)`` with a resolvable
``sender`` emits a derived CALLS edge from the sender's model class to the
receiver, tagged with the signal names. The decorator text is parsed with
``ast.parse`` (never evaluated).

Known limits (v1):
  - Only the ``@receiver`` decorator, not ``signal.connect(handler, ...)``.
  - Only senders that name a class (``Model``, ``"app.Model"``,
    ``Model.field.through``). ``settings.AUTH_USER_MODEL``, a variable or
    no sender at all (custom signals) are skipped.
  - A class name defined more than once resolves only when exactly one of
    the definitions lives under a ``models`` module; otherwise it is skipped.
"""

from __future__ import annotations

import ast
import json
import logging
from typing import TYPE_CHECKING, Optional

from .parser import EdgeInfo

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

_DERIVED_FLAG = "django_signal_resolved"


def _clear_derived_signal_edges(store: GraphStore) -> int:
    """Remove previously derived edges before recomputing them.

    The rebuild is global whenever Python changes, like the other Django and
    Spring resolvers: a removed receiver or a changed sender must not leave a
    stale edge whose owning file was not reparsed.
    """
    rows = store._conn.execute("SELECT id, extra FROM edges WHERE kind = 'CALLS'").fetchall()
    stale: list[tuple[int]] = []
    for row in rows:
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if extra.get(_DERIVED_FLAG):
            stale.append((row["id"],))
    if stale:
        store._conn.executemany("DELETE FROM edges WHERE id = ?", stale)
        store.commit()
    return len(stale)


def _names(node: ast.expr) -> list[str]:
    """Signal names from the first ``receiver`` argument (a name or a list)."""
    items = node.elts if isinstance(node, (ast.List, ast.Tuple)) else [node]
    out = []
    for item in items:
        if isinstance(item, ast.Name):
            out.append(item.id)
        elif isinstance(item, ast.Attribute):
            out.append(item.attr)
    return out


def _sender_class(node: ast.expr) -> Optional[str]:
    """The class a ``sender=`` value names, or None when it names none."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        name = node.value.rsplit(".", 1)[-1]  # "app_label.ModelName"
    elif isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        # Model.field.through, models.Model: the first part that looks like
        # a class name.
        parts: list[str] = []
        cur: ast.expr = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        candidates = [p for p in reversed(parts) if p[:1].isupper() and not p.isupper()]
        name = candidates[0] if candidates else ""
    else:
        return None
    # SETTINGS_STYLE constants are not classes.
    if not name or name.isupper() or not name[:1].isupper():
        return None
    return name


def parse_receiver(decorator: str) -> Optional[tuple[list[str], Optional[str]]]:
    """``(signals, sender class)`` from a ``receiver(...)`` decorator string."""
    text = decorator.strip().lstrip("@")
    if not text.split("(", 1)[0].endswith("receiver"):
        return None
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return None
    call = tree.body
    if not isinstance(call, ast.Call) or not call.args:
        return None
    signals = _names(call.args[0])
    sender = next((_sender_class(k.value) for k in call.keywords if k.arg == "sender"), None)
    return (signals, sender) if signals else None


def _class_index(store: GraphStore) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for row in store._conn.execute(
        "SELECT name, qualified_name FROM nodes WHERE kind = 'Class' AND language = 'python'"
    ):
        index.setdefault(row["name"], []).append(row["qualified_name"])
    return index


def _resolve_class(name: str, index: dict[str, list[str]]) -> Optional[str]:
    found = index.get(name, [])
    if len(found) == 1:
        return found[0]
    in_models = [qn for qn in found if "/models" in qn.replace("\\", "/").split("::")[0]]
    return in_models[0] if len(in_models) == 1 else None


def resolve_django_signals(store: GraphStore) -> dict[str, int]:
    """Rebuild the derived sender -> receiver CALLS edges."""
    removed = _clear_derived_signal_edges(store)
    index = _class_index(store)
    rows = store._conn.execute(
        "SELECT qualified_name, file_path, line_start, extra FROM nodes "
        "WHERE kind = 'Function' AND language = 'python' AND extra LIKE '%receiver(%'"
    ).fetchall()
    receivers = emitted = unresolved = 0
    for row in rows:
        try:
            decorators = json.loads(row["extra"] or "{}").get("decorators") or []
        except (json.JSONDecodeError, TypeError):
            continue
        by_sender: dict[str, list[str]] = {}
        for decorator in decorators:
            parsed = parse_receiver(str(decorator))
            if parsed is None:
                continue
            receivers += 1
            signals, sender = parsed
            sender_qn = _resolve_class(sender, index) if sender else None
            if sender_qn is None:
                unresolved += 1
                continue
            by_sender.setdefault(sender_qn, []).extend(signals)
        for sender_qn, signals in by_sender.items():
            store.upsert_edge(EdgeInfo(
                kind="CALLS",
                source=sender_qn,
                target=row["qualified_name"],
                file_path=row["file_path"],
                line=row["line_start"] or 0,
                extra={
                    _DERIVED_FLAG: True,
                    "django_signal": ",".join(dict.fromkeys(signals)),
                    "resolution": "django_signal_receiver",
                    "confidence": 0.9,
                    "confidence_tier": "INFERRED",
                },
            ))
            emitted += 1
    store.commit()
    logger.info(
        "Django signal resolver: %d receivers, %d edges, %d unresolved senders",
        receivers, emitted, unresolved,
    )
    return {
        "receivers_seen": receivers,
        "edges_emitted": emitted,
        "unresolved_senders": unresolved,
        "stale_edges_removed": removed,
    }

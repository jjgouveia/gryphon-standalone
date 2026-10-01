"""Tests for the Django signal-receiver resolver (gryphon/django_signal_resolver.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gryphon.diff_context import build_diff_context
from gryphon.django_signal_resolver import parse_receiver, resolve_django_signals
from gryphon.graph import GraphStore
from gryphon.parser import NodeInfo


@pytest.mark.parametrize(
    ("decorator", "expected"),
    [
        ("receiver(pre_save, sender=Document)", (["pre_save"], "Document")),
        ('receiver(post_save, sender="docs.Document")', (["post_save"], "Document")),
        ("@receiver([post_save, post_delete], sender=Order)",
         (["post_save", "post_delete"], "Order")),
        ("receiver(m2m_changed, sender=CustomUser.user_permissions.through)",
         (["m2m_changed"], "CustomUser")),
        ("receiver(signals.post_save, sender=models.Invoice)", (["post_save"], "Invoice")),
        ("receiver(post_save, sender=settings.AUTH_USER_MODEL)", (["post_save"], None)),
        ("receiver(order_paid)", (["order_paid"], None)),
        ("login_required", None),
        ("receiver(", None),
    ],
)
def test_parse_receiver(decorator, expected):
    assert parse_receiver(decorator) == expected


def _fn(name, path, line, decorators=()):
    extra = {"decorators": list(decorators)} if decorators else {}
    return NodeInfo(kind="Function", name=name, file_path=path, line_start=line,
                    line_end=line + 3, language="python", extra=extra)


def _cls(name, path, line=1):
    return NodeInfo(kind="Class", name=name, file_path=path, line_start=line,
                    line_end=line + 10, language="python")


@pytest.fixture
def store(tmp_path):
    s = GraphStore(tmp_path / "graph.db")
    yield s
    s.close()


def _signal_edges(store):
    return sorted(
        (e.source_qualified.rsplit("::", 1)[-1], e.target_qualified.rsplit("::", 1)[-1],
         e.extra.get("django_signal"))
        for e in (store.get_edges_by_source(row["qualified_name"])
                  for row in store._conn.execute("SELECT qualified_name FROM nodes"))
        for e in e
        if e.extra.get("django_signal_resolved")
    )


def test_resolver_links_senders_to_receivers(store):
    store.upsert_node(_cls("Document", "/r/app/models/document.py"))
    store.upsert_node(_cls("Order", "/r/app/models.py"))
    store.upsert_node(_fn("capture", "/r/app/signals.py", 10,
                          ['receiver(pre_save, sender="docs.Document")']))
    store.upsert_node(_fn("notify", "/r/app/signals.py", 20,
                          ["receiver([post_save, post_delete], sender=Order)"]))
    store.upsert_node(_fn("on_user", "/r/app/signals.py", 30,
                          ["receiver(post_save, sender=settings.AUTH_USER_MODEL)"]))
    stats = resolve_django_signals(store)
    assert _signal_edges(store) == [
        ("Document", "capture", "pre_save"),
        ("Order", "notify", "post_save,post_delete"),
    ]
    assert stats["receivers_seen"] == 3 and stats["unresolved_senders"] == 1


def test_resolver_is_idempotent_and_drops_stale_edges(store):
    store.upsert_node(_cls("Order", "/r/app/models.py"))
    store.upsert_node(_fn("notify", "/r/app/signals.py", 20, ["receiver(post_save, sender=Order)"]))
    resolve_django_signals(store)
    resolve_django_signals(store)
    assert len(_signal_edges(store)) == 1
    # The receiver loses its decorator: the edge must go.
    store.upsert_node(_fn("notify", "/r/app/signals.py", 20))
    stats = resolve_django_signals(store)
    assert _signal_edges(store) == [] and stats["stale_edges_removed"] == 1


def test_ambiguous_sender_prefers_the_models_module(store):
    store.upsert_node(_cls("Order", "/r/app/models.py"))
    store.upsert_node(_cls("Order", "/r/app/serializers.py"))
    store.upsert_node(_cls("Ticket", "/r/a/views.py"))
    store.upsert_node(_cls("Ticket", "/r/b/views.py"))
    store.upsert_node(_fn("a", "/r/app/signals.py", 1, ["receiver(post_save, sender=Order)"]))
    store.upsert_node(_fn("b", "/r/app/signals.py", 9, ["receiver(post_save, sender=Ticket)"]))
    resolve_django_signals(store)
    assert _signal_edges(store) == [("Order", "a", "post_save")]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=str(repo), capture_output=True, text=True, check=True).stdout


def test_diff_context_shows_signal_callers_and_sibling_receivers(tmp_path, store):
    """The pilot case: a changed receiver, and the pre_save receiver sharing its model."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "signals.py").write_text(
        "def capture(sender, instance, **kw):\n    instance._before = 1\n\n\n"
        "def on_saved(sender, instance, **kw):\n    return instance._before\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "signals.py").write_text(
        "def capture(sender, instance, **kw):\n    instance._before = 1\n\n\n"
        "def on_saved(sender, instance, **kw):\n    return instance._before + 1\n",
        encoding="utf-8",
    )
    _git(repo, "commit", "-q", "-am", "change receiver")
    root = repo.as_posix()
    store.upsert_node(_cls("Document", f"{root}/models.py", 3))
    store.upsert_node(_fn("capture", f"{root}/signals.py", 1,
                          ["receiver(pre_save, sender=Document)"]))
    store.upsert_node(_fn("on_saved", f"{root}/signals.py", 5,
                          ["receiver(post_save, sender=Document)"]))
    resolve_django_signals(store)

    text = build_diff_context(str(repo), ["main...feat"], store=store)
    assert "- on_saved (signals.py:5) <- Document (via post_save) (models.py:3)" in text
    assert "on_saved on Document: also capture [pre_save] (signals.py:1)" in text
    assert "none found statically" not in text

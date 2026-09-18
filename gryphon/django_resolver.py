"""Resolve DRF ViewSet routes to close the routed test-gap blind spot.

A test that hits a ViewSet action via ``self.client.post(url)`` leaves no
CALLS edge to the handler method: the URL string is resolved by Django's
router at runtime, invisible to static analysis. Without this resolver,
``detect_changes``/``get_review_context`` report the handler as an
untested "test gap" even when a test exercises it end to end via HTTP —
and separately flag ``@pytest.fixture`` functions the same way, which
``changes.py``'s ``_is_pytest_fixture`` handles; this module only closes
the routing gap.

Three pieces, captured during normal parsing (see ``parser.py``):
  - ``django_router_registrations`` on File nodes: ``router.register()``
    calls found in that file (normally ``urls.py``).
  - ``drf_action`` on Function nodes: ``@action(...)`` or a default
    ViewSet action name (list/create/retrieve/update/partial_update/
    destroy), with the enclosing class required to look like a ViewSet.
  - ``django_client_calls`` on Test nodes: ``client.<verb>(url, ...)``
    calls found in the test's own source.

This module joins the three by URL-segment compatibility and emits the
missing TESTED_BY edge directly (bypassing the generic CALLS-derived
TESTED_BY logic, since there is no real CALLS edge to derive it from).

Known limits (v1):
  - Only DRF ViewSets registered via ``router.register()`` — not raw
    ``path()``/``re_path()`` view functions, and not Flask/Express/other
    frameworks (those still show as un-closeable test gaps).
  - Only literal or single-level f-string test URLs — a URL built via
    ``reverse()``, string concatenation, or a variable computed earlier
    in the test will not match.
  - Doesn't account for an ``include()`` prefix added by a parent
    ``urls.py`` — the registered prefix is matched as a suffix of the
    test URL, which tolerates an unknown leading prefix but cannot
    distinguish two ViewSets registered under the same trailing segment
    in different parent includes.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Optional

from .parser import EdgeInfo

if TYPE_CHECKING:
    from .graph import GraphNode, GraphStore

logger = logging.getLogger(__name__)

_DERIVED_FLAG = "django_route_resolved"


def _clear_derived_route_edges(store: GraphStore) -> int:
    """Remove previously-derived TESTED_BY edges before recomputing them.

    The rebuild is global whenever Python changes, mirroring the Spring
    event resolver: a renamed action, a removed registration, or an
    edited test URL must not leave a stale edge whose owning file was not
    itself reparsed.
    """
    rows = store._conn.execute(
        "SELECT id, extra FROM edges WHERE kind = 'TESTED_BY'",
    ).fetchall()
    derived_ids: list[tuple[int]] = []
    for row in rows:
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if extra.get(_DERIVED_FLAG):
            derived_ids.append((row["id"],))
    if derived_ids:
        store._conn.executemany("DELETE FROM edges WHERE id = ?", derived_ids)
    return len(derived_ids)


def _segments(path: str) -> list[Optional[str]]:
    """Split a route/URL into segments; a segment holding a placeholder
    becomes ``None`` (matches anything). Covers Django path converters
    (``<int:pk>``, ``<pk>``) and f-string interpolations (``{due_id}``).
    """
    result: list[Optional[str]] = []
    for seg in path.strip("/").split("/"):
        if not seg:
            continue
        if (seg.startswith("<") and seg.endswith(">")) or "{" in seg:
            result.append(None)
        else:
            result.append(seg)
    return result


def _route_segments(prefix: str, action: dict[str, Any]) -> list[Optional[str]]:
    segs = _segments(prefix)
    if action.get("detail"):
        segs.append(None)
    url_path = action.get("url_path") or ""
    if url_path:
        segs.extend(_segments(url_path))
    return segs


def _matches(route: list[Optional[str]], call_url: list[Optional[str]]) -> bool:
    """Whether every registered-route segment is compatible with the
    tail of the test's URL. The test URL may carry an unknown leading
    prefix (tenant slug, API version, an outer ``include()``), so this
    matches the route against the URL's *suffix* rather than requiring
    the whole thing to line up. See the "Known limits" note above.
    """
    if len(route) > len(call_url):
        return False
    call_tail = call_url[len(call_url) - len(route):]
    return all(
        a is None or b is None or a == b
        for a, b in zip(route, call_tail)
    )


def resolve_django_routes(store: GraphStore) -> dict[str, int]:
    """Match test client HTTP calls to DRF ViewSet actions; emit TESTED_BY.

    Returns ``{"django_routes_matched": N, "django_edges_removed": M}``.
    """
    removed = _clear_derived_route_edges(store)

    registrations: list[dict[str, Any]] = []
    for f in store.get_nodes_by_kind(["File"]):
        for reg in (f.extra or {}).get("django_router_registrations") or []:
            registrations.append(reg)
    if not registrations:
        store.commit()
        return {"django_routes_matched": 0, "django_edges_removed": removed}

    actions: list[GraphNode] = [
        n for n in store.get_nodes_by_kind(["Function"])
        if (n.extra or {}).get("drf_action")
    ]
    tests: list[GraphNode] = [
        n for n in store.get_nodes_by_kind(["Test"])
        if (n.extra or {}).get("django_client_calls")
    ]
    if not actions or not tests:
        store.commit()
        return {"django_routes_matched": 0, "django_edges_removed": removed}

    actions_by_class: dict[str, list[GraphNode]] = {}
    for n in actions:
        if n.parent_name:
            bare = n.parent_name.rsplit(".", 1)[-1]
            actions_by_class.setdefault(bare, []).append(n)

    routes: list[tuple[list[Optional[str]], GraphNode]] = []
    for reg in registrations:
        for action_node in actions_by_class.get(reg["viewset"], []):
            routes.append((
                _route_segments(reg["prefix"], action_node.extra["drf_action"]),
                action_node,
            ))
    if not routes:
        store.commit()
        return {"django_routes_matched": 0, "django_edges_removed": removed}

    matched = 0
    for test_node in tests:
        for call in test_node.extra["django_client_calls"]:
            call_segments = _segments(call["url"])
            for route_segments, action_node in routes:
                if not _matches(route_segments, call_segments):
                    continue
                store.upsert_edge(EdgeInfo(
                    kind="TESTED_BY",
                    source=action_node.qualified_name,
                    target=test_node.qualified_name,
                    file_path=test_node.file_path,
                    line=test_node.line_start,
                    extra={
                        _DERIVED_FLAG: True,
                        "resolution": "django_route_match",
                        "http_method": call["http_method"],
                        "confidence": 0.7,
                        "confidence_tier": "INFERRED",
                    },
                ))
                matched += 1

    store.commit()
    logger.info(
        "Django route resolver: matched %d test-to-action edge(s)", matched,
    )
    return {"django_routes_matched": matched, "django_edges_removed": removed}

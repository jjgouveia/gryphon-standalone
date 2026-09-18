"""Tests for the DRF route resolver (django_resolver.py).

Mirrors the shape of test_spring_events.py: a routed-test-gap blind spot
closed the same way Spring's application-event publisher/listener gap
is — capture metadata during parsing, join it in a postprocessing pass,
emit the missing edge.
"""

from pathlib import Path

from gryphon.graph import GraphStore
from gryphon.incremental import full_build, incremental_update
from gryphon.parser import CodeParser


def _parse_python(path: Path, source: str):
    return CodeParser().parse_bytes(path, source.encode())


def test_router_register_call_is_captured_on_file_extra(tmp_path: Path) -> None:
    path = tmp_path / "urls.py"
    nodes, _ = _parse_python(
        path,
        """
        router = DefaultRouter()
        router.register(r"due-diligences", DueDiligenceV2ViewSet, basename="due-diligence")
        """,
    )
    file_node = next(n for n in nodes if n.kind == "File")
    regs = file_node.extra["django_router_registrations"]
    assert regs == [{
        "prefix": "due-diligences",
        "viewset": "DueDiligenceV2ViewSet",
        "basename": "due-diligence",
    }]


def test_action_decorator_and_default_action_captured_on_viewset_methods(
    tmp_path: Path,
) -> None:
    path = tmp_path / "views.py"
    nodes, _ = _parse_python(
        path,
        """
        class DueDiligenceV2ViewSet(viewsets.ModelViewSet):
            @action(detail=True, methods=["post"])
            def desarquivar(self, request, pk=None):
                pass

            def retrieve(self, request, pk=None):
                pass

            def not_routed(self):
                pass
        """,
    )
    by_name = {n.name: n for n in nodes if n.kind == "Function"}
    assert by_name["desarquivar"].extra["drf_action"] == {
        "url_path": "desarquivar", "detail": True,
    }
    assert by_name["retrieve"].extra["drf_action"] == {
        "url_path": "", "detail": True,
    }
    assert "drf_action" not in by_name["not_routed"].extra


def test_client_calls_captured_on_test_functions(tmp_path: Path) -> None:
    path = tmp_path / "test_thing.py"
    nodes, _ = _parse_python(
        path,
        '''
        def test_desarquivar_restores_status(api_client_v2, due_v2):
            resp = api_client_v2.post(
                f"/api/v2/due-diligences/{due_v2.id}/desarquivar/",
                {"motivo": "x"},
                format="json",
            )
            assert resp.status_code == 200
        ''',
    )
    test_node = next(n for n in nodes if n.kind == "Test")
    assert test_node.extra["django_client_calls"] == [
        {"http_method": "post", "url": "/api/v2/due-diligences/{due_v2.id}/desarquivar/"},
    ]


def _write_drf_project(root: Path) -> None:
    (root / "urls.py").write_text(
        'router.register(r"due-diligences", DueDiligenceV2ViewSet, basename="due-diligence")\n',
        encoding="utf-8",
    )
    (root / "views.py").write_text(
        "class DueDiligenceV2ViewSet(viewsets.ModelViewSet):\n"
        "    @action(detail=True, methods=['post'])\n"
        "    def desarquivar(self, request, pk=None):\n"
        "        pass\n",
        encoding="utf-8",
    )
    (root / "test_desarquivar.py").write_text(
        "def test_desarquivar_restores_status(client, due_id):\n"
        "    resp = client.post(f\"/api/v2/due-diligences/{due_id}/desarquivar/\")\n"
        "    assert resp.status_code == 200\n",
        encoding="utf-8",
    )


def test_full_build_emits_tested_by_edge_for_routed_action(tmp_path: Path) -> None:
    _write_drf_project(tmp_path)
    graph_dir = tmp_path / ".gryphon"
    graph_dir.mkdir()

    with GraphStore(graph_dir / "graph.db") as store:
        result = full_build(tmp_path, store)
        assert result["django_route_resolution"]["django_routes_matched"] == 1

        handler = store.get_node(
            f"{(tmp_path / 'views.py').as_posix()}::DueDiligenceV2ViewSet.desarquivar",
        )
        assert handler is not None
        tested_by = [
            e for e in store.get_edges_by_source(handler.qualified_name)
            if e.kind == "TESTED_BY"
        ]
        assert len(tested_by) == 1
        assert tested_by[0].extra["resolution"] == "django_route_match"


def test_incremental_action_rename_removes_stale_route_edge(tmp_path: Path) -> None:
    _write_drf_project(tmp_path)
    graph_dir = tmp_path / ".gryphon"
    graph_dir.mkdir()

    with GraphStore(graph_dir / "graph.db") as store:
        first = full_build(tmp_path, store)
        assert first["django_route_resolution"]["django_routes_matched"] == 1

        (tmp_path / "views.py").write_text(
            "class DueDiligenceV2ViewSet(viewsets.ModelViewSet):\n"
            "    @action(detail=True, methods=['post'])\n"
            "    def desarquivar_renamed(self, request, pk=None):\n"
            "        pass\n",
            encoding="utf-8",
        )
        updated = incremental_update(tmp_path, store, changed_files=["views.py"])

        assert updated["django_route_resolution"]["django_routes_matched"] == 0
        handler = store.get_node(
            f"{(tmp_path / 'views.py').as_posix()}::DueDiligenceV2ViewSet.desarquivar_renamed",
        )
        assert handler is not None
        assert not [
            e for e in store.get_edges_by_source(handler.qualified_name)
            if e.kind == "TESTED_BY"
        ]


def test_no_match_when_router_registration_absent(tmp_path: Path) -> None:
    """A ViewSet action with no router.register() anywhere in the repo has
    nothing to resolve — the resolver must no-op, not guess."""
    (tmp_path / "views.py").write_text(
        "class OrphanViewSet(viewsets.ModelViewSet):\n"
        "    @action(detail=True, methods=['post'])\n"
        "    def desarquivar(self, request, pk=None):\n"
        "        pass\n",
        encoding="utf-8",
    )
    (tmp_path / "test_orphan.py").write_text(
        "def test_desarquivar(client, due_id):\n"
        "    resp = client.post(f\"/api/v2/orphans/{due_id}/desarquivar/\")\n"
        "    assert resp.status_code == 200\n",
        encoding="utf-8",
    )
    graph_dir = tmp_path / ".gryphon"
    graph_dir.mkdir()
    with GraphStore(graph_dir / "graph.db") as store:
        result = full_build(tmp_path, store)
        assert result["django_route_resolution"]["django_routes_matched"] == 0

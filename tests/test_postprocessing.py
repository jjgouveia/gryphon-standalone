"""Tests for the shared post-processing pipeline."""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from gryphon.graph import GraphStore
from gryphon.incremental import full_build, incremental_update
from gryphon.parser import EdgeInfo, NodeInfo
from gryphon.postprocessing import run_post_processing


def _get_signature(store, qualified_name):
    row = store._conn.execute(
        "SELECT signature FROM nodes WHERE qualified_name = ?",
        (qualified_name,),
    ).fetchone()
    return row["signature"] if row else None


class TestRunPostProcessing:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()  # release the handle before GraphStore reopens it on Windows
        self.store = GraphStore(self.tmp.name)
        self._seed_data()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed_data(self):
        self.store.upsert_node(
            NodeInfo(
                kind="File",
                name="/repo/app.py",
                file_path="/repo/app.py",
                line_start=1,
                line_end=50,
                language="python",
            )
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Class",
                name="Service",
                file_path="/repo/app.py",
                line_start=5,
                line_end=40,
                language="python",
            )
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="handle",
                file_path="/repo/app.py",
                line_start=10,
                line_end=20,
                language="python",
                parent_name="Service",
                params="request",
                return_type="Response",
            )
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="process",
                file_path="/repo/app.py",
                line_start=25,
                line_end=35,
                language="python",
            )
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Test",
                name="test_handle",
                file_path="/repo/test_app.py",
                line_start=1,
                line_end=10,
                language="python",
                is_test=True,
            )
        )

        self.store.upsert_edge(
            EdgeInfo(
                kind="CONTAINS",
                source="/repo/app.py",
                target="/repo/app.py::Service",
                file_path="/repo/app.py",
            )
        )
        self.store.upsert_edge(
            EdgeInfo(
                kind="CONTAINS",
                source="/repo/app.py::Service",
                target="/repo/app.py::Service.handle",
                file_path="/repo/app.py",
            )
        )
        self.store.upsert_edge(
            EdgeInfo(
                kind="CALLS",
                source="/repo/app.py::Service.handle",
                target="/repo/app.py::process",
                file_path="/repo/app.py",
                line=15,
            )
        )
        self.store.commit()

    def test_computes_signatures(self):
        unsigned = self.store.get_nodes_without_signature()
        assert len(unsigned) > 0

        result = run_post_processing(self.store)

        assert result["signatures_computed"] > 0
        remaining = self.store.get_nodes_without_signature()
        assert len(remaining) == 0

    def test_function_signature_format(self):
        run_post_processing(self.store)

        sig = _get_signature(self.store, "/repo/app.py::Service.handle")
        assert sig == "def handle(request) -> Response"

    def test_class_signature_format(self):
        run_post_processing(self.store)

        sig = _get_signature(self.store, "/repo/app.py::Service")
        assert sig == "class Service"

    def test_test_signature_format(self):
        run_post_processing(self.store)

        sig = _get_signature(self.store, "/repo/test_app.py::test_handle")
        assert sig is not None
        assert sig.startswith("def test_handle(")

    def test_rebuilds_fts_index(self):
        result = run_post_processing(self.store)

        assert "fts_indexed" in result
        assert result["fts_indexed"] > 0

    def test_fts_search_works_after_post_processing(self):
        run_post_processing(self.store)

        from gryphon.search import hybrid_search

        hits = hybrid_search(self.store, "handle")
        names = {h["name"] for h in hits}
        assert "handle" in names

    def test_detects_flows(self):
        result = run_post_processing(self.store)

        assert "flows_detected" in result
        assert result["flows_detected"] >= 0

    def test_detects_communities(self):
        result = run_post_processing(self.store)

        assert "communities_detected" in result
        assert result["communities_detected"] >= 0

    def test_no_warnings_on_healthy_store(self):
        result = run_post_processing(self.store)

        assert "warnings" not in result

    def test_empty_store_no_crash(self):
        empty_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        empty_tmp.close()  # release the handle before GraphStore reopens it on Windows
        empty_store = GraphStore(empty_tmp.name)
        try:
            result = run_post_processing(empty_store)
            assert result["signatures_computed"] == 0
            assert result["fts_indexed"] == 0
        finally:
            empty_store.close()
            Path(empty_tmp.name).unlink(missing_ok=True)

    def test_idempotent(self):
        first = run_post_processing(self.store)
        second = run_post_processing(self.store)

        assert second["fts_indexed"] == first["fts_indexed"]
        assert second["signatures_computed"] == 0

    def test_signature_truncated_at_512(self):
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="f",
                file_path="/repo/big.py",
                line_start=1,
                line_end=2,
                language="python",
                params="a" * 600,
            )
        )
        self.store.commit()

        run_post_processing(self.store)
        sig = _get_signature(self.store, "/repo/big.py::f")
        assert sig is not None
        assert len(sig) <= 512


class TestPostProcessingStepIsolation:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()  # release the handle before GraphStore reopens it on Windows
        self.store = GraphStore(self.tmp.name)
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="fn",
                file_path="/repo/a.py",
                line_start=1,
                line_end=5,
                language="python",
            )
        )
        self.store.commit()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_fts_failure_does_not_block_flows(self):
        with patch(
            "gryphon.search.rebuild_fts_index",
            side_effect=ImportError("fts boom"),
        ):
            result = run_post_processing(self.store)

        assert "flows_detected" in result
        assert "communities_detected" in result
        assert "warnings" in result
        assert any("FTS" in w for w in result["warnings"])

    def test_flow_failure_does_not_block_communities(self):
        with patch(
            "gryphon.flows.trace_flows",
            side_effect=ImportError("flow boom"),
        ):
            result = run_post_processing(self.store)

        assert "communities_detected" in result
        assert "warnings" in result
        assert any("Flow" in w for w in result["warnings"])

    def test_community_failure_still_has_signatures(self):
        with patch(
            "gryphon.communities.detect_communities",
            side_effect=ImportError("comm boom"),
        ):
            result = run_post_processing(self.store)

        assert result["signatures_computed"] > 0
        assert "warnings" in result
        assert any("Community" in w for w in result["warnings"])


class TestToolBuildUsesSharedPipeline:
    def test_build_tool_runs_post_processing(self, tmp_path):
        py_file = tmp_path / "sample.py"
        py_file.write_text("def hello():\n    pass\n")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".gryphon").mkdir()

        db_path = tmp_path / ".gryphon" / "graph.db"
        store = GraphStore(db_path)
        try:
            mock_target = "gryphon.incremental.get_all_tracked_files"
            with patch(mock_target, return_value=["sample.py"]):
                full_build(tmp_path, store)

            unsigned_before_pp = store.get_nodes_without_signature()
            run_post_processing(store)
            unsigned_after_pp = store.get_nodes_without_signature()

            assert len(unsigned_before_pp) > 0
            assert len(unsigned_after_pp) == 0
        finally:
            store.close()

    def test_src_layout_imports_resolve_before_test_coverage(self, tmp_path):
        runner = tmp_path / "src" / "mypkg" / "runner.py"
        test_file = tmp_path / "tests" / "test_runner.py"
        runner.parent.mkdir(parents=True)
        test_file.parent.mkdir()
        (runner.parent / "__init__.py").write_text("")
        runner.write_text(
            "def render_thing(code: str) -> str:\n"
            "    return code.upper()\n"
        )
        test_file.write_text(
            "from mypkg.runner import render_thing\n\n"
            "def test_render_thing_basic():\n"
            "    assert render_thing('a') == 'A'\n\n"
            "def test_pipeline_uses_uppercase():\n"
            "    assert render_thing('bc') == 'BC'\n"
        )
        (tmp_path / ".git").mkdir()
        graph_dir = tmp_path / ".gryphon"
        graph_dir.mkdir()

        store = GraphStore(graph_dir / "graph.db")
        try:
            tracked = [
                "src/mypkg/__init__.py",
                "src/mypkg/runner.py",
                "tests/test_runner.py",
            ]
            with patch(
                "gryphon.incremental.get_all_tracked_files",
                return_value=tracked,
            ):
                result = full_build(tmp_path, store)
            assert result["python_resolution"]["imports_resolved"] == 1
            assert {
                row["target_qualified"]
                for row in store._conn.execute(
                    "SELECT target_qualified FROM edges "
                    "WHERE kind = 'IMPORTS_FROM' AND file_path = ?",
                    (test_file.as_posix(),),
                ).fetchall()
            } == {runner.as_posix()}

            run_post_processing(store)
            production = f"{runner.as_posix()}::render_thing"
            tests = store.get_transitive_tests(production, max_depth=0)
            assert {test["name"] for test in tests} == {
                "test_render_thing_basic",
                "test_pipeline_uses_uppercase",
            }

            duplicate = tmp_path / "packages" / "other" / "src" / "mypkg" / "runner.py"
            duplicate.parent.mkdir(parents=True)
            duplicate.write_text(runner.read_text())
            update = incremental_update(
                tmp_path,
                store,
                changed_files=["packages/other/src/mypkg/runner.py"],
            )
            assert update["python_resolution"]["imports_ambiguous"] == 1
            imported = store._conn.execute(
                "SELECT target_qualified, extra FROM edges "
                "WHERE kind = 'IMPORTS_FROM' AND file_path = ?",
                (test_file.as_posix(),),
            ).fetchone()
            assert imported["target_qualified"] == "mypkg.runner"
            assert '"import_resolution": "ambiguous"' in imported["extra"]

            run_post_processing(store)
            assert store.get_transitive_tests(production, max_depth=0) == []

            duplicate.unlink()
            update = incremental_update(
                tmp_path,
                store,
                changed_files=["packages/other/src/mypkg/runner.py"],
            )
            assert update["python_resolution"]["imports_resolved"] == 1
            imported = store._conn.execute(
                "SELECT target_qualified FROM edges "
                "WHERE kind = 'IMPORTS_FROM' AND file_path = ?",
                (test_file.as_posix(),),
            ).fetchone()
            assert imported["target_qualified"] == runner.as_posix()

            run_post_processing(store)
            tests = store.get_transitive_tests(production, max_depth=0)
            assert {test["name"] for test in tests} == {
                "test_render_thing_basic",
                "test_pipeline_uses_uppercase",
            }
        finally:
            store.close()

    def test_initial_ambiguous_python_import_has_no_claimed_caller(self, tmp_path):
        """A graph first built with duplicate module suffixes must stay ambiguous."""
        from gryphon.tools.query import query_graph

        production_files = []
        for package in ("a", "b"):
            runner = (
                tmp_path
                / "packages"
                / package
                / "src"
                / "mypkg"
                / "runner.py"
            )
            runner.parent.mkdir(parents=True)
            runner.write_text(
                "def render_thing(code: str) -> str:\n"
                "    return code.upper()\n"
            )
            production_files.append(runner)

        test_file = tmp_path / "tests" / "test_runner.py"
        test_file.parent.mkdir()
        test_file.write_text(
            "from mypkg.runner import render_thing\n\n"
            "def test_pipeline():\n"
            "    assert render_thing('bc') == 'BC'\n"
        )
        (tmp_path / ".git").mkdir()
        graph_dir = tmp_path / ".gryphon"
        graph_dir.mkdir()
        tracked = [
            *(path.relative_to(tmp_path).as_posix() for path in production_files),
            "tests/test_runner.py",
        ]

        store = GraphStore(graph_dir / "graph.db")
        try:
            with patch(
                "gryphon.incremental.get_all_tracked_files",
                return_value=tracked,
            ):
                result = full_build(tmp_path, store)
            assert result["python_resolution"]["imports_ambiguous"] == 1
            run_post_processing(store)

            import_edge = store._conn.execute(
                "SELECT target_qualified, extra FROM edges "
                "WHERE kind = 'IMPORTS_FROM' AND file_path = ?",
                (test_file.as_posix(),),
            ).fetchone()
            assert import_edge["target_qualified"] == "mypkg.runner"
            assert '"import_resolution": "ambiguous"' in import_edge["extra"]

            endpoint_edges = store._conn.execute(
                "SELECT kind, extra FROM edges "
                "WHERE kind IN ('CALLS', 'TESTED_BY') AND file_path = ?",
                (test_file.as_posix(),),
            ).fetchall()
            assert {row["kind"] for row in endpoint_edges} == {
                "CALLS",
                "TESTED_BY",
            }
            assert all(
                '"ambiguous_target_count": 2' in row["extra"]
                for row in endpoint_edges
            )

            for runner in production_files:
                callers = query_graph(
                    pattern="callers_of",
                    target=f"{runner.as_posix()}::render_thing",
                    repo_root=str(tmp_path),
                )
                assert callers["results"] == []
        finally:
            store.close()


class TestPythonSubmoduleImports:
    """``from pkg import submodule [as alias]`` imports the submodule's file.

    A test calling ``alias.func()`` must count as covering ``func``; before, the
    edge stopped at ``pkg/__init__.py`` and the call stayed a bare name, so the
    PR report listed a covered function as a test gap.
    """

    @staticmethod
    def _build(tmp_path, test_source: str):
        merge = tmp_path / "pkg" / "merge.py"
        helpers = tmp_path / "pkg" / "helpers.py"
        test_file = tmp_path / "tests" / "test_merge.py"
        merge.parent.mkdir(parents=True)
        test_file.parent.mkdir()
        (merge.parent / "__init__.py").write_text("")
        merge.write_text("def split_findings(items):\n    return list(items)\n")
        helpers.write_text("def helper():\n    return 1\n")
        test_file.write_text(test_source)
        (tmp_path / ".git").mkdir()
        (tmp_path / ".gryphon").mkdir()
        store = GraphStore(tmp_path / ".gryphon" / "graph.db")
        tracked = [
            "pkg/__init__.py", "pkg/merge.py", "pkg/helpers.py", "tests/test_merge.py",
        ]
        with patch("gryphon.incremental.get_all_tracked_files", return_value=tracked):
            full_build(tmp_path, store)
        run_post_processing(store)
        return store, merge, helpers, test_file

    @staticmethod
    def _import_targets(store, test_file) -> set[str]:
        return {
            row["target_qualified"]
            for row in store._conn.execute(
                "SELECT target_qualified FROM edges "
                "WHERE kind = 'IMPORTS_FROM' AND file_path = ?",
                (test_file.as_posix(),),
            ).fetchall()
        }

    def test_aliased_submodule_call_counts_as_test_coverage(self, tmp_path):
        store, merge, _helpers, test_file = self._build(
            tmp_path,
            "from pkg import merge as merge_mod\n\n"
            "def test_split_keeps_items():\n"
            "    assert merge_mod.split_findings([1]) == [1]\n",
        )
        try:
            production = f"{merge.as_posix()}::split_findings"
            assert merge.as_posix() in self._import_targets(store, test_file)
            tests = store.get_transitive_tests(production, max_depth=0)
            assert [t["name"] for t in tests] == ["test_split_keeps_items"]
            # What detect-changes reads: a TESTED_BY edge on the qualified function.
            assert any(
                e.kind == "TESTED_BY" for e in store.get_edges_by_source(production)
            )
        finally:
            store.close()

    def test_plain_submodule_import_and_several_names(self, tmp_path):
        store, merge, helpers, test_file = self._build(
            tmp_path,
            "from pkg import merge, helpers as h\n\n"
            "def test_both():\n"
            "    assert merge.split_findings([]) == [] and h.helper() == 1\n",
        )
        try:
            targets = self._import_targets(store, test_file)
            assert {merge.as_posix(), helpers.as_posix()} <= targets
            for module, name in ((merge, "split_findings"), (helpers, "helper")):
                assert any(
                    e.kind == "TESTED_BY"
                    for e in store.get_edges_by_source(f"{module.as_posix()}::{name}")
                )
        finally:
            store.close()

    def test_importing_a_function_adds_no_module_edge(self, tmp_path):
        store, merge, helpers, test_file = self._build(
            tmp_path,
            "from pkg.merge import split_findings\n\n"
            "def test_split():\n"
            "    assert split_findings([2]) == [2]\n",
        )
        try:
            targets = self._import_targets(store, test_file)
            assert targets == {merge.as_posix()}  # no edge to helpers.py or a bogus module
        finally:
            store.close()

    def test_a_name_that_is_not_a_module_adds_nothing(self, tmp_path):
        store, merge, helpers, test_file = self._build(
            tmp_path,
            "from pkg import helpers_missing\n\n"
            "def test_x():\n"
            "    assert True\n",
        )
        try:
            assert self._import_targets(store, test_file) == {
                (tmp_path / "pkg" / "__init__.py").as_posix(),
            }
        finally:
            store.close()


class TestWatchCallbackIntegration:
    def test_watch_accepts_callback_parameter(self):
        import inspect

        from gryphon.incremental import watch

        sig = inspect.signature(watch)
        assert "on_files_updated" in sig.parameters

    def test_watch_callback_not_called_without_updates(self, tmp_path):
        from gryphon.incremental import watch

        (tmp_path / ".git").mkdir()
        db_path = tmp_path / "test.db"
        store = GraphStore(db_path)
        callback = MagicMock()

        try:
            with (
                patch("watchdog.observers.Observer") as observer,
                patch("time.sleep", side_effect=KeyboardInterrupt),
            ):
                watch(tmp_path, store, on_files_updated=callback)

            callback.assert_not_called()
            observer.return_value.start.assert_called_once()
            observer.return_value.stop.assert_called()
            observer.return_value.join.assert_called_once()
        finally:
            store.close()

    def test_watch_deletion_reresolves_python_imports(self, tmp_path):
        from gryphon.incremental import full_build, watch

        runner = tmp_path / "src" / "mypkg" / "runner.py"
        duplicate = tmp_path / "packages" / "other" / "src" / "mypkg" / "runner.py"
        test_file = tmp_path / "tests" / "test_runner.py"
        runner.parent.mkdir(parents=True)
        duplicate.parent.mkdir(parents=True)
        test_file.parent.mkdir()
        (runner.parent / "__init__.py").write_text("")
        runner.write_text("def render_thing(code: str) -> str:\n    return code.upper()\n")
        duplicate.write_text(runner.read_text())
        test_file.write_text(
            "from mypkg.runner import render_thing\n\n"
            "def test_render_thing():\n"
            "    assert render_thing('a') == 'A'\n"
        )
        (tmp_path / ".git").mkdir()
        store = GraphStore(tmp_path / "graph.db")
        observer = MagicMock()

        try:
            tracked = [
                "src/mypkg/__init__.py",
                "src/mypkg/runner.py",
                "packages/other/src/mypkg/runner.py",
                "tests/test_runner.py",
            ]
            with patch(
                "gryphon.incremental.get_all_tracked_files",
                return_value=tracked,
            ):
                full_build(tmp_path, store)
            run_post_processing(store)
            duplicate.unlink()

            with (
                patch("watchdog.observers.Observer", return_value=observer),
                patch("time.sleep", side_effect=KeyboardInterrupt),
            ):
                watch(tmp_path, store, on_files_updated=run_post_processing)

            imported = store._conn.execute(
                "SELECT target_qualified FROM edges "
                "WHERE kind = 'IMPORTS_FROM' AND file_path = ?",
                (test_file.as_posix(),),
            ).fetchone()
            assert imported["target_qualified"] == runner.as_posix()
            tests = store.get_transitive_tests(
                f"{runner.as_posix()}::render_thing",
                max_depth=0,
            )
            assert {test["name"] for test in tests} == {"test_render_thing"}
        finally:
            store.close()


class TestResolveBareEndpointsStep:
    """The shared/watch pipeline resolves evidence-backed bare endpoints."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = GraphStore(self.tmp.name)
        self._seed_bare_edges()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed_bare_edges(self):
        app_file = "/repo/src/app.py"
        util_file = "/repo/src/util.py"
        test_file = "/repo/tests/test_app.py"
        for name, path, is_test in [
            ("parse", app_file, False),
            ("helper", util_file, False),
            ("test_parse", test_file, True),
        ]:
            self.store.upsert_node(NodeInfo(
                kind="Test" if is_test else "Function",
                name=name,
                file_path=path,
                line_start=1,
                line_end=5,
                language="python",
                is_test=is_test,
            ))
        for imported in (app_file, util_file):
            self.store.upsert_edge(EdgeInfo(
                kind="IMPORTS_FROM",
                source=test_file,
                target=imported,
                file_path=test_file,
                line=1,
            ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS",
            source=f"{test_file}::test_parse",
            target="helper",
            file_path=test_file,
            line=2,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="TESTED_BY",
            source="parse",
            target=f"{test_file}::test_parse",
            file_path=test_file,
            line=3,
        ))
        self.store.commit()

    def test_resolves_before_derived_steps_and_reports_count(self):
        result = run_post_processing(self.store)

        assert result["bare_edges_resolved"] == 2
        rows = self.store._conn.execute(
            "SELECT kind, source_qualified, target_qualified FROM edges "
            "WHERE kind IN ('CALLS', 'TESTED_BY') ORDER BY kind"
        ).fetchall()
        by_kind = {
            row["kind"]: (
                row["source_qualified"], row["target_qualified"],
            )
            for row in rows
        }
        assert by_kind["CALLS"] == (
            "/repo/tests/test_app.py::test_parse",
            "/repo/src/util.py::helper",
        )
        assert by_kind["TESTED_BY"] == (
            "/repo/src/app.py::parse",
            "/repo/tests/test_app.py::test_parse",
        )

    def test_resolution_failure_is_a_warning_not_a_pipeline_failure(self):
        with patch.object(
            GraphStore,
            "resolve_bare_call_targets",
            side_effect=sqlite3.OperationalError("boom"),
        ):
            result = run_post_processing(self.store)

        assert "bare_edges_resolved" not in result
        assert any("Call-target resolution" in w for w in result["warnings"])
        assert "communities_detected" in result


def test_post_processing_steps_run_on_the_calling_thread(monkeypatch, tmp_path):
    """Regression: two steps ran in threads on the store's one SQLite connection.

    A connection does not isolate transactions per thread, so one step's
    BEGIN/COMMIT interleaved with the other's ("cannot rollback - no
    transaction is active") and bare-call edges went silently missing.
    """
    import threading

    from gryphon import postprocessing
    from gryphon.graph import GraphStore

    caller = threading.get_ident()
    seen: dict[str, int] = {}

    def recorder(name):
        def step(store, *args, **kwargs):
            seen[name] = threading.get_ident()
            return {}, []
        return step

    for name in ("_resolve_bare_endpoints", "_compute_signatures", "_rebuild_fts_index",
                 "_trace_flows", "_detect_communities"):
        monkeypatch.setattr(postprocessing, name, recorder(name))
    store = GraphStore(tmp_path / "graph.db")
    try:
        postprocessing.run_post_processing(store)
    finally:
        store.close()
    assert {"_resolve_bare_endpoints", "_compute_signatures"} <= set(seen)
    assert set(seen.values()) == {caller}

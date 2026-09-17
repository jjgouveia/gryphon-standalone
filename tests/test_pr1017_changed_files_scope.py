"""An explicit ``changed_files`` list must define what gets analysed.

Reviewing a remote PR means naming the changed files yourself: the branch is
not checked out, so the local working tree knows nothing about them. Before
this fix, ``analyze_changes`` derived ranges from the working-tree diff
whenever ``repo_root`` was given and then ignored ``changed_files`` entirely,
so the review described whatever the local checkout happened to be diffing.
``get_minimal_context_tool`` compounded it by reporting the repo's globally
top-ranked flows and communities under names that claim to describe the
change (``flows_affected``).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from gryphon.changes import analyze_changes
from gryphon.graph import GraphStore
from gryphon.parser import NodeInfo


class _Store:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()  # Windows needs the handle released before reopening
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _add(self, abs_path: str, name: str, kind: str = "Function") -> None:
        self.store.upsert_node(
            NodeInfo(
                kind=kind, name=name, file_path=abs_path,
                line_start=1, line_end=10, language="python",
            ),
            file_hash="abc",
        )
        self.store.commit()


class TestExplicitChangedFilesWin(_Store):
    def test_working_tree_diff_never_widens_the_caller_s_file_list(self, tmp_path):
        """The PR's file is analysed; the unrelated local edit is not."""
        pr_file = (tmp_path / "broker.py").as_posix()
        local_file = (tmp_path / "certidoes.py").as_posix()
        self._add(pr_file, "resolve_broker_row_action")
        self._add(local_file, "use_view_model_certidoes")

        # The local checkout is mid-edit on an unrelated file.
        with patch(
            "gryphon.changes.parse_diff_ranges",
            return_value={"certidoes.py": [(1, 5)]},
        ):
            result = analyze_changes(
                self.store,
                changed_files=["broker.py"],
                repo_root=str(tmp_path),
            )

        names = {f["name"] for f in result["changed_functions"]}
        assert "resolve_broker_row_action" in names
        assert "use_view_model_certidoes" not in names

    def test_partial_overlap_keeps_every_named_file(self, tmp_path):
        """A file the diff does cover and one it does not both get analysed."""
        seen_file = (tmp_path / "seen.py").as_posix()
        unseen_file = (tmp_path / "unseen.py").as_posix()
        noise_file = (tmp_path / "noise.py").as_posix()
        self._add(seen_file, "seen_fn")
        self._add(unseen_file, "unseen_fn")
        self._add(noise_file, "noise_fn")

        with patch(
            "gryphon.changes.parse_diff_ranges",
            return_value={"seen.py": [(1, 5)], "noise.py": [(1, 5)]},
        ):
            result = analyze_changes(
                self.store,
                changed_files=["seen.py", "unseen.py"],
                repo_root=str(tmp_path),
            )

        names = {f["name"] for f in result["changed_functions"]}
        assert names == {"seen_fn", "unseen_fn"}

    def test_a_node_reachable_both_ways_is_reported_once(self, tmp_path):
        """Range-matched and whole-file nodes dedup on qualified_name."""
        path = (tmp_path / "both.py").as_posix()
        self._add(path, "only_fn")

        with patch(
            "gryphon.changes.parse_diff_ranges",
            return_value={"both.py": [(1, 5)]},
        ):
            result = analyze_changes(
                self.store,
                changed_files=["both.py"],
                repo_root=str(tmp_path),
            )

        assert [f["name"] for f in result["changed_functions"]] == ["only_fn"]

    def test_caller_supplied_ranges_still_define_their_own_scope(self, tmp_path):
        """Explicit changed_ranges are left unmapped, so they are not scoped."""
        self._add("app.py", "rel_func")

        result = analyze_changes(
            self.store,
            changed_files=["app.py"],
            changed_ranges={"app.py": [(2, 3)]},
            repo_root=str(tmp_path),
        )

        assert any(f["name"] == "rel_func" for f in result["changed_functions"])

    def test_empty_ranges_mean_no_ranges_available_not_analyse_nothing(self, tmp_path):
        """``changed_ranges={}`` is the whole-file fallback, not an empty scope.

        Scoping the ranges must not swallow this: it is how callers say the
        diff produced no usable ranges for the files they named (#852).
        """
        path = (tmp_path / "services.py").as_posix()
        self._add(path, "service_fn")

        result = analyze_changes(
            self.store,
            changed_files=["services.py"],
            changed_ranges={},
            repo_root=str(tmp_path),
        )

        assert [f["name"] for f in result["changed_functions"]] == ["service_fn"]

    def test_no_changed_files_falls_back_to_the_working_tree(self, tmp_path):
        """Callers that name nothing still get the local diff, as before."""
        local_file = (tmp_path / "local.py").as_posix()
        self._add(local_file, "local_fn")

        with patch(
            "gryphon.changes.parse_diff_ranges",
            return_value={"local.py": [(1, 5)]},
        ):
            result = analyze_changes(
                self.store, changed_files=[], repo_root=str(tmp_path),
            )

        assert [f["name"] for f in result["changed_functions"]] == ["local_fn"]


def _seed_two_area_repo(tmp_path: Path) -> Path:
    """A repo with two disjoint areas.

    ``certidoes`` holds the deeper (so more critical) flow and the larger
    community; ``broker`` is what the PR touches. Any field that claims to
    describe the change has to name broker, never certidoes.
    """
    from gryphon.flows import store_flows, trace_flows
    from gryphon.graph import EdgeInfo

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gryphon").mkdir()
    for name in ("broker.py", "broker_helper.py", "certidoes.py", "certidoes_helper.py"):
        (repo / name).write_text("# pad\n" * 50, encoding="utf-8")

    paths = {n: (repo / n).as_posix() for n in (
        "broker.py", "broker_helper.py", "certidoes.py", "certidoes_helper.py",
    )}
    store = GraphStore(str(repo / ".gryphon" / "graph.db"))
    try:
        def fn(path: str, name: str, line: int) -> None:
            store.upsert_node(NodeInfo(
                kind="Function", name=name, file_path=path,
                line_start=line, line_end=line + 1, language="python",
            ))

        fn(paths["broker.py"], "resolve_broker_row_action", 1)
        fn(paths["broker_helper.py"], "to_broker_row_action_input", 1)
        # A longer chain, so this flow outranks broker's on criticality.
        fn(paths["certidoes.py"], "use_view_model_certidoes", 1)
        fn(paths["certidoes_helper.py"], "certidao_card", 1)
        fn(paths["certidoes_helper.py"], "certidao_anexo_delete_dialog", 10)

        def calls(src_path: str, src: str, dst_path: str, dst: str, line: int) -> None:
            store.upsert_edge(EdgeInfo(
                kind="CALLS",
                source=f"{src_path}::{src}", target=f"{dst_path}::{dst}",
                file_path=src_path, line=line,
            ))

        calls(paths["broker.py"], "resolve_broker_row_action",
              paths["broker_helper.py"], "to_broker_row_action_input", 2)
        calls(paths["certidoes.py"], "use_view_model_certidoes",
              paths["certidoes_helper.py"], "certidao_card", 2)
        calls(paths["certidoes_helper.py"], "certidao_card",
              paths["certidoes_helper.py"], "certidao_anexo_delete_dialog", 11)
        store.commit()

        store_flows(store, trace_flows(store))

        # Communities: certidoes is the bigger one, so a repo-wide "top by
        # size" query would always name it.
        store._conn.execute(
            "INSERT INTO communities (id, name, level, size) VALUES (?, ?, 0, ?)",
            (1, "broker-area", 2),
        )
        store._conn.execute(
            "INSERT INTO communities (id, name, level, size) VALUES (?, ?, 0, ?)",
            (2, "certidoes-area", 99),
        )
        for path, community in (
            (paths["broker.py"], 1), (paths["broker_helper.py"], 1),
            (paths["certidoes.py"], 2), (paths["certidoes_helper.py"], 2),
        ):
            store._conn.execute(
                "UPDATE nodes SET community_id = ? WHERE file_path = ?",
                (community, path),
            )
        store.commit()
    finally:
        store.close()
    return repo


class TestMinimalContextDescribesTheChange:
    """``flows_affected`` and ``communities`` must be about the change set."""

    def _context(self, repo: Path) -> dict:
        from gryphon.tools.context import get_minimal_context

        return get_minimal_context(
            task="review PR #3473",
            changed_files=["broker.py", "broker_helper.py"],
            repo_root=str(repo),
        )

    def test_flows_affected_names_the_changed_area(self, tmp_path):
        repo = _seed_two_area_repo(tmp_path)

        result = self._context(repo)

        flows = " ".join(result.get("flows_affected") or [])
        assert "broker" in flows, result.get("flows_affected")
        assert "certidao" not in flows and "certidoes" not in flows, (
            result.get("flows_affected")
        )

    def test_communities_name_the_changed_area(self, tmp_path):
        repo = _seed_two_area_repo(tmp_path)

        result = self._context(repo)

        assert result.get("communities") == ["broker-area"], result.get("communities")

    def test_key_entities_come_from_the_named_files(self, tmp_path):
        repo = _seed_two_area_repo(tmp_path)

        result = self._context(repo)

        entities = result.get("key_entities") or []
        assert any("broker" in e for e in entities), entities
        assert not any("certidao" in e for e in entities), entities

    def test_without_a_change_set_the_repo_wide_view_is_still_returned(self, tmp_path):
        """No changed files means the question is "what is this repo", so the
        repo-wide top communities remain the honest answer."""
        from gryphon.tools.context import get_minimal_context

        repo = _seed_two_area_repo(tmp_path)

        result = get_minimal_context(task="onboard", repo_root=str(repo))

        assert result.get("communities") == ["certidoes-area", "broker-area"]


class TestDetectChangesToolScope(_Store):
    """The MCP tool is the path the review skills actually take.

    The scoping fix lives in ``analyze_changes``, but ``detect_changes_func``
    used to parse the working-tree diff itself and pass it as
    ``changed_ranges``. That marked the local diff as caller-supplied scope,
    which is exempt from scoping, so the tool kept reporting the local
    checkout's entities under the PR's file names - with a risk score and
    test gaps to match. The library-level tests all passed while the only
    real caller stayed broken, so these pin the tool.
    """

    def _run(self, tmp_path, changed_files, local_diff, **kwargs):
        from gryphon.tools import detect_changes_func

        with (
            patch(
                "gryphon.tools.review._get_store",
                return_value=(self.store, tmp_path),
            ),
            patch(
                "gryphon.tools.review.resolve_review_base",
                return_value="merge-base-sha",
            ),
            patch(
                "gryphon.changes.parse_diff_ranges", return_value=local_diff,
            ),
            # The tool used to hold its own binding and parse the diff
            # itself. Patching only ``gryphon.changes`` leaves that call
            # hitting a non-repo tmp_path, which returns {} and hides the
            # very leak these tests exist to catch.
            patch(
                "gryphon.tools.review.parse_diff_ranges",
                return_value=local_diff,
            ),
            patch.object(self.store, "close"),
        ):
            return detect_changes_func(
                changed_files=changed_files,
                repo_root=str(tmp_path),
                **kwargs,
            )

    def test_local_diff_does_not_leak_into_a_named_change_set(self, tmp_path):
        pr_file = (tmp_path / "broker.py").as_posix()
        local_file = (tmp_path / "certidoes.py").as_posix()
        self._add(pr_file, "resolve_broker_row_action")
        self._add(local_file, "use_view_model_certidoes")

        result = self._run(
            tmp_path,
            ["broker.py"],
            {"certidoes.py": [(1, 5)]},
        )

        names = [f["name"] for f in result["changed_functions"]]
        assert names == ["resolve_broker_row_action"]
        assert "use_view_model_certidoes" not in names

    def test_files_the_graph_never_saw_are_reported_not_scored_as_clean(
        self, tmp_path,
    ):
        """A 0.00 over nothing must not look like a 0.00 over a safe change."""
        result = self._run(tmp_path, ["broker.ts", "gaveta.ts"], {})

        assert result["risk_score"] == 0.0
        assert result["files_in_graph"] == 0
        assert result["files_not_in_graph"] == 2
        assert "graph" in result["confidence"]
        assert "rebuild" in result["confidence"]
        assert "Graph coverage" in result["summary"]

    def test_the_warning_survives_detail_level_minimal(self, tmp_path):
        """The review skills call with minimal, so it has to land there too."""
        result = self._run(
            tmp_path, ["broker.ts"], {}, detail_level="minimal",
        )

        assert result["files_not_in_graph"] == 1
        assert "rebuild" in result["confidence"]

    def test_a_partly_indexed_change_set_reports_the_shortfall(self, tmp_path):
        """The harder case: a plausible score computed over half the diff."""
        indexed = (tmp_path / "broker.py").as_posix()
        self._add(indexed, "resolve_broker_row_action")

        result = self._run(tmp_path, ["broker.py", "gaveta.ts"], {})

        assert result["files_in_graph"] == 1
        assert result["files_not_in_graph"] == 1
        assert "1 of 2" in result["confidence"]

    def test_a_fully_indexed_change_set_carries_no_warning(self, tmp_path):
        indexed = (tmp_path / "broker.py").as_posix()
        self._add(indexed, "resolve_broker_row_action")

        result = self._run(tmp_path, ["broker.py"], {})

        assert result["files_in_graph"] == 1
        assert result["files_not_in_graph"] == 0
        assert "confidence" not in result
        assert "Graph coverage" not in result["summary"]

    def test_zero_coverage_is_not_ready_rather_than_a_clean_review(
        self, tmp_path,
    ):
        """An advisory note lost to the agent's own cost/benefit judgement."""
        result = self._run(tmp_path, ["broker.ts"], {})

        assert result["status"] == "not_ready"
        assert "rebuild" in result["reason"]
        assert result["next_tool_suggestions"] == ["build_or_update_graph"]

    def test_partial_coverage_still_returns_its_partial_answer(self, tmp_path):
        """Half an answer beats none; the shortfall is on the response."""
        self._add((tmp_path / "broker.py").as_posix(), "resolve_broker_row")

        result = self._run(tmp_path, ["broker.py", "gaveta.ts"], {})

        assert result["status"] == "ok"
        assert "1 of 2" in result["confidence"]

"""Measured savings must be earned on files the graph can answer for.

``measure_savings`` credited the graph with the whole difference between
reading every impacted file and reading the tool response, regardless of how
much of the diff the graph actually held. A review the graph could not inform
still logged a six-figure counterfactual saving, which is the number the
savings dashboard reports.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from gryphon.graph import GraphStore
from gryphon.measure import measure_savings
from gryphon.parser import NodeInfo


@pytest.fixture
def repo(tmp_path):
    """A repo whose files exist on disk, with a store that can be seeded."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()  # Windows needs the handle released before reopening
    store = GraphStore(tmp.name)

    def write(name: str, body: str = "x = 1\n" * 200) -> str:
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        return path.as_posix()

    def index(abs_path: str, name: str) -> None:
        store.upsert_node(
            NodeInfo(
                kind="Function", name=name, file_path=abs_path,
                line_start=1, line_end=10, language="python",
            ),
            file_hash="abc",
        )
        store.commit()

    yield store, tmp_path, write, index
    store.close()
    Path(tmp.name).unlink(missing_ok=True)


def _measure(store, root, changed_files):
    with patch("gryphon.measure._get_store", return_value=(store, root)):
        return measure_savings(
            repo_root=str(root), changed_files=changed_files, log=False,
        )


def test_a_change_set_the_graph_never_saw_earns_no_savings(repo):
    store, root, write, _ = repo
    write("broker.ts")
    write("gaveta.ts")

    result = _measure(store, root, ["broker.ts", "gaveta.ts"])

    assert result["files_in_graph"] == 0
    assert result["graph_coverage"] == 0.0
    assert result["complete"] is False
    assert result["saved_tokens"] == 0
    assert result["counterfactual_saved"] == 0


def test_a_partly_indexed_change_set_is_discounted_not_rounded_up(repo):
    store, root, write, index = repo
    indexed = write("broker.py")
    write("gaveta.ts")
    index(indexed, "resolve_broker_row_action")

    result = _measure(store, root, ["broker.py", "gaveta.ts"])

    assert result["files_in_graph"] == 1
    assert result["files_not_in_graph"] == 1
    assert result["graph_coverage"] == 0.5
    assert result["complete"] is False


def test_a_fully_indexed_change_set_is_not_discounted(repo):
    store, root, write, index = repo
    indexed = write("broker.py")
    index(indexed, "resolve_broker_row_action")

    result = _measure(store, root, ["broker.py"])

    assert result["files_in_graph"] == 1
    assert result["graph_coverage"] == 1.0
    assert result["complete"] is True


def test_coverage_is_reported_separately_from_the_tokenizer_signal(repo):
    """``verified`` is the dashboard's tiktoken label, not a coverage claim.

    Folding coverage into it would make the dashboard report a chars/4
    estimate for a count tiktoken produced exactly.
    """
    store, root, write, _ = repo
    write("broker.ts")

    with patch("gryphon.measure._load_tiktoken", return_value=None):
        result = _measure(store, root, ["broker.ts"])

    assert result["verified"] is False  # no tiktoken
    assert result["complete"] is False  # and nothing indexed
    assert "graph_coverage" in result

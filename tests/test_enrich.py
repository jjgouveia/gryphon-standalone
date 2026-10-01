"""Tests for the PreToolUse search enrichment module."""

import tempfile
from pathlib import Path

from gryphon.enrich import (
    enrich_file_read,
    enrich_search,
    extract_pattern,
)
from gryphon.graph import GraphStore
from gryphon.parser import EdgeInfo, NodeInfo
from gryphon.search import rebuild_fts_index


class TestExtractPattern:
    def test_grep_pattern(self):
        assert extract_pattern("Grep", {"pattern": "parse_file"}) == "parse_file"

    def test_grep_empty(self):
        assert extract_pattern("Grep", {}) is None

    def test_glob_meaningful_name(self):
        assert extract_pattern("Glob", {"pattern": "**/auth*.ts"}) == "auth"

    def test_glob_pure_extension(self):
        assert extract_pattern("Glob", {"pattern": "**/*.ts"}) is None

    def test_glob_short_name(self):
        # "ab" is only 2 chars, below minimum regex match of 3
        assert extract_pattern("Glob", {"pattern": "**/ab.ts"}) is None

    def test_bash_rg_pattern(self):
        result = extract_pattern("Bash", {"command": "rg parse_file src/"})
        assert result == "parse_file"

    def test_bash_grep_pattern(self):
        result = extract_pattern("Bash", {"command": "grep -r 'GraphStore' ."})
        assert result == "GraphStore"

    def test_bash_rg_with_flags(self):
        result = extract_pattern("Bash", {"command": "rg -t py -i parse_file"})
        assert result == "parse_file"

    def test_bash_non_grep_command(self):
        assert extract_pattern("Bash", {"command": "ls -la"}) is None

    def test_bash_short_pattern(self):
        # Pattern "ab" is only 2 chars
        assert extract_pattern("Bash", {"command": "rg ab src/"}) is None

    def test_unknown_tool(self):
        assert extract_pattern("Write", {"content": "hello"}) is None

    def test_bash_rg_with_glob_flag(self):
        result = extract_pattern(
            "Bash", {"command": "rg --glob '*.py' parse_file"}
        )
        assert result == "parse_file"


class TestEnrichSearch:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_dir = Path(self.tmpdir) / ".gryphon"
        self.db_dir.mkdir()
        self.db_path = self.db_dir / "graph.db"
        self.store = GraphStore(self.db_path)
        self._seed_data()

    def teardown_method(self):
        self.store.close()

    def _seed_data(self):
        # POSIX spelling matches graph identity on every platform (#774).
        posix_dir = Path(self.tmpdir).as_posix()
        nodes = [
            NodeInfo(
                kind="Function", name="parse_file", file_path=f"{posix_dir}/parser.py",
                line_start=10, line_end=50, language="python",
                params="(path: str)", return_type="list[Node]",
            ),
            NodeInfo(
                kind="Function", name="full_build", file_path=f"{posix_dir}/build.py",
                line_start=1, line_end=30, language="python",
            ),
            NodeInfo(
                kind="Test", name="test_parse_file",
                file_path=f"{posix_dir}/test_parser.py",
                line_start=1, line_end=20, language="python",
                is_test=True,
            ),
        ]
        for n in nodes:
            self.store.upsert_node(n)
        edges = [
            EdgeInfo(
                kind="CALLS",
                source=f"{posix_dir}/build.py::full_build",
                target=f"{posix_dir}/parser.py::parse_file",
                file_path=f"{posix_dir}/build.py", line=15,
            ),
            EdgeInfo(
                # TESTED_BY edges are stored as source=production, target=test
                # by the parser. See: #515
                kind="TESTED_BY",
                source=f"{posix_dir}/parser.py::parse_file",
                target=f"{posix_dir}/test_parser.py::test_parse_file",
                file_path=f"{posix_dir}/test_parser.py", line=1,
            ),
        ]
        for e in edges:
            self.store.upsert_edge(e)
        rebuild_fts_index(self.store)

    def test_returns_matching_symbols(self):
        result = enrich_search("parse_file", self.tmpdir)
        assert "[gryphon]" in result
        assert "parse_file" in result

    def test_includes_callers(self):
        result = enrich_search("parse_file", self.tmpdir)
        assert "Called by:" in result
        assert "full_build" in result

    def test_includes_tests(self):
        result = enrich_search("parse_file", self.tmpdir)
        assert "Tests:" in result
        assert "test_parse_file" in result

    def test_excludes_test_nodes(self):
        result = enrich_search("test_parse", self.tmpdir)
        # test nodes should be filtered out of results
        assert "test_parse_file" not in result or "symbol(s)" in result

    def test_empty_for_no_match(self):
        result = enrich_search("nonexistent_function_xyz", self.tmpdir)
        assert result == ""

    def test_empty_for_missing_db(self):
        result = enrich_search("parse_file", "/tmp/nonexistent_repo_xyz")
        assert result == ""


class TestEnrichFileRead:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_dir = Path(self.tmpdir) / ".gryphon"
        self.db_dir.mkdir()
        self.db_path = self.db_dir / "graph.db"
        self.store = GraphStore(self.db_path)
        self._seed_data()

    def teardown_method(self):
        self.store.close()

    def _seed_data(self):
        # POSIX spelling matches graph identity on every platform (#774).
        self.file_path = (Path(self.tmpdir) / "parser.py").as_posix()
        nodes = [
            NodeInfo(
                kind="File", name="parser.py", file_path=self.file_path,
                line_start=1, line_end=100, language="python",
            ),
            NodeInfo(
                kind="Function", name="parse_file", file_path=self.file_path,
                line_start=10, line_end=50, language="python",
            ),
            NodeInfo(
                kind="Function", name="parse_imports", file_path=self.file_path,
                line_start=55, line_end=80, language="python",
            ),
        ]
        for n in nodes:
            self.store.upsert_node(n)
        edges = [
            EdgeInfo(
                kind="CALLS",
                source=f"{self.file_path}::parse_file",
                target=f"{self.file_path}::parse_imports",
                file_path=self.file_path, line=30,
            ),
        ]
        for e in edges:
            self.store.upsert_edge(e)
        self.store._conn.commit()

    def test_returns_file_symbols(self):
        result = enrich_file_read(self.file_path, self.tmpdir)
        assert "[gryphon]" in result
        assert "parse_file" in result
        assert "parse_imports" in result

    def test_excludes_file_nodes(self):
        result = enrich_file_read(self.file_path, self.tmpdir)
        # File node "parser.py" should not appear as a symbol entry
        lines = result.split("\n")
        symbol_lines = [
            ln for ln in lines
            if ln and not ln.startswith(" ") and not ln.startswith("[")
        ]
        for line in symbol_lines:
            assert "parser.py (" not in line or "parse_" in line

    def test_leaves_out_what_the_code_already_shows(self):
        """Callees are in the code being read; flows did not help a review."""
        result = enrich_file_read(self.file_path, self.tmpdir)
        assert "Calls:" not in result and "Flows:" not in result
        assert "Called by: parse_file" in result  # parse_imports' caller

    def test_empty_for_unknown_file(self):
        result = enrich_file_read("/nonexistent/file.py", self.tmpdir)
        assert result == ""

    def test_empty_for_missing_db(self):
        result = enrich_file_read(self.file_path, "/tmp/nonexistent_repo_xyz")
        assert result == ""


class TestRunHookOutput:
    """Test the JSON output format of run_hook via enrich_search."""

    def test_hook_json_format(self):
        """Verify the hookSpecificOutput structure is correct."""
        # We test the format indirectly by checking enrich_search output
        # since run_hook reads from stdin which is harder to test
        tmpdir = tempfile.mkdtemp()
        db_dir = Path(tmpdir) / ".gryphon"
        db_dir.mkdir()
        store = GraphStore(db_dir / "graph.db")
        store.upsert_node(
            NodeInfo(
                kind="Function", name="my_function",
                file_path=f"{tmpdir}/mod.py",
                line_start=1, line_end=10, language="python",
            ),
        )
        rebuild_fts_index(store)
        store.close()

        result = enrich_search("my_function", tmpdir)
        assert result.startswith("[gryphon]")
        assert "my_function" in result


def test_enrich_uses_the_configured_data_dir(tmp_path, monkeypatch):
    """A graph kept outside the repo (CRG_DATA_DIR) is found, like everywhere else."""
    repo = tmp_path / "repo"
    repo.mkdir()
    data_dir = tmp_path / "external-data"
    data_dir.mkdir()
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    posix_repo = repo.as_posix()
    store = GraphStore(data_dir / "graph.db")
    try:
        store.upsert_node(NodeInfo(
            kind="Function", name="parse_file", file_path=f"{posix_repo}/parser.py",
            line_start=1, line_end=5, language="python",
        ))
        rebuild_fts_index(store)
    finally:
        store.close()

    assert not (repo / ".gryphon").exists()
    assert "parse_file" in enrich_search("parse_file", str(repo))
    assert "parse_file" in enrich_file_read(f"{posix_repo}/parser.py", str(repo))


# --- review-aware enrichment ------------------------------------------------

import json  # noqa: E402

import pytest  # noqa: E402

from gryphon import enrich as enrich_mod  # noqa: E402
from gryphon.enrich import (  # noqa: E402
    _SessionMemory,
    build_context,
    extract_file_reads,
    extract_search_terms,
)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (r'grep -n "def load_config\|def save_config" a.py',
         ["load_config", "save_config"]),
        ('grep -rn "class OrderStatus" -A25 src', ["OrderStatus"]),
        (r'grep -n "^from\|^import\|ValidationError" views.py', ["ValidationError"]),
        ("git grep -n price_ladder -- tests", ["price_ladder"]),
        ("rg -e parse_file src/", ["parse_file"]),
        ('grep -n "def" a.py', []),
        ("ls -la", []),
    ],
)
def test_extract_search_terms(command, expected):
    assert extract_search_terms("Bash", {"command": command}) == expected


def test_extract_pattern_respects_quotes():
    """Regression: `.split()` turned "class Foo" into the pattern `class`."""
    assert extract_pattern("Bash", {"command": 'grep -rn "class Foo" src'}) == "class Foo"


@pytest.mark.parametrize(
    ("tool", "tool_input", "expected"),
    [
        ("Bash", {"command": "sed -n 420,625p a/serializers.py"}, [("a/serializers.py", 420, 625)]),
        # After `cd x`, relative paths are under x (agents write `cd dir && sed ...`).
        ("Bash", {"command": "cd x; sed -n '12p' b.py; sed -n 1,5p c.py"},
         [("x/b.py", 12, 12), ("x/c.py", 1, 5)]),
        ("Bash", {"command": "cd src/app && cd sub && head -n 5 m.py"},
         [("src/app/sub/m.py", 1, 5)]),
        ("Bash", {"command": "cd src && cat /abs/m.py; cd; cat n.py"},
         [("/abs/m.py", None, None), ("n.py", None, None)]),
        ("Bash", {"command": "head -n 40 a.py"}, [("a.py", 1, 40)]),
        ("Bash", {"command": "head -20 a.py"}, [("a.py", 1, 20)]),
        ("Bash", {"command": "cat a.py b.py"}, [("a.py", None, None), ("b.py", None, None)]),
        ("Bash", {"command": "sed 's/a/b/' a.py"}, []),
        ("Read", {"file_path": "/r/a.py", "offset": 100, "limit": 50}, [("/r/a.py", 100, 149)]),
        ("Read", {"file_path": "/r/a.py"}, [("/r/a.py", None, None)]),
        ("Grep", {"pattern": "x"}, []),
    ],
)
def test_extract_file_reads(tool, tool_input, expected):
    assert extract_file_reads(tool, tool_input) == expected


def test_session_memory_dedups_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CRG_DATA_DIR", str(tmp_path / "data"))
    first = _SessionMemory(str(tmp_path), "sess-1")
    assert first.filter(["a", "b"]) == {"a", "b"}
    assert first.filter(["b", "c"]) == {"c"}
    again = _SessionMemory(str(tmp_path), "sess-1")
    assert again.filter(["a", "d"]) == {"d"}
    assert _SessionMemory(str(tmp_path), "sess-2").filter(["a"]) == {"a"}
    # No session id: no memory, nothing suppressed.
    assert _SessionMemory(str(tmp_path), None).filter(["a"]) == {"a"}


def test_post_tool_use_git_diff_calls_diff_context_once(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CRG_DATA_DIR", str(tmp_path / "data"))
    calls = []

    def fake_context(repo_root, args, *, cwd=None, store=None):
        calls.append((args, cwd))
        return "[gryphon] diff context"

    monkeypatch.setattr("gryphon.diff_context.build_diff_context", fake_context)
    payload = {"hook_event_name": "PostToolUse", "session_id": "s", "tool_name": "Bash",
               "tool_input": {"command": "git diff --stat main...feat"}, "cwd": str(tmp_path)}
    assert build_context(payload, str(tmp_path)) == ("PostToolUse", "[gryphon] diff context")
    assert build_context(payload, str(tmp_path)) == ("PostToolUse", "")
    assert calls == [(["main...feat"], str(tmp_path))]
    other = {**payload, "tool_input": {"command": "pytest -q"}}
    assert build_context(other, str(tmp_path)) == ("PostToolUse", "")


def test_run_hook_reports_the_event_it_answers(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CRG_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    GraphStore(tmp_path / "data" / "graph.db").close()
    monkeypatch.setattr(enrich_mod, "build_context", lambda hook, root: ("PostToolUse", "ctx"))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(
        {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {}},
    )))
    enrich_mod.run_hook(repo=str(tmp_path))
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"] == {"hookEventName": "PostToolUse", "additionalContext": "ctx"}


def test_file_range_only_covers_the_lines_read(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    (repo / ".gryphon").mkdir(parents=True)
    root = repo.as_posix()
    store = GraphStore(repo / ".gryphon" / "graph.db")
    try:
        for name, lo, hi in (("first", 1, 10), ("second", 20, 30)):
            store.upsert_node(NodeInfo(kind="Function", name=name, file_path=f"{root}/m.py",
                                       line_start=lo, line_end=hi, language="python"))
    finally:
        store.close()
    text = enrich_mod.enrich_file_range("m.py", str(repo), 22, 25)
    assert "second" in text and "first" not in text
    assert enrich_mod.enrich_file_range("m.py", str(repo), 11, 19) == ""


def test_windows_absolute_path_is_not_joined_to_the_cd_directory():
    # Quoted, as bash needs it: unquoted, bash itself reads the backslash as an escape.
    reads = extract_file_reads("Bash", {"command": r'cd src && cat "C:\repo\m.py"'})
    assert reads == [(r"C:\repo\m.py", None, None)]


def _range_store(tmp_path, monkeypatch, nodes, edges=()):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    (repo / ".gryphon").mkdir(parents=True)
    store = GraphStore(repo / ".gryphon" / "graph.db")
    try:
        for n in nodes:
            store.upsert_node(n)
        for e in edges:
            store.upsert_edge(e)
    finally:
        store.close()
    return repo


def test_no_static_caller_is_said_not_omitted(tmp_path, monkeypatch):
    """An empty "Called by" read as dead code for signal receivers."""
    root = (tmp_path / "repo").as_posix()
    repo = _range_store(tmp_path, monkeypatch, [
        NodeInfo(kind="Function", name="on_save", file_path=f"{root}/signals.py",
                 line_start=1, line_end=4, language="python"),
        NodeInfo(kind="Type", name="Payload", file_path=f"{root}/signals.py",
                 line_start=6, line_end=8, language="python"),
    ])
    text = enrich_mod.enrich_file_range("signals.py", str(repo), 1, 8)
    on_save, payload = text.split("Payload", 1)
    assert "Called by: none found statically" in on_save
    assert "signals, decorators and dynamic dispatch" in on_save
    assert "Called by" not in payload  # types are not "called"


def test_unique_name_only_edge_counts_as_a_caller(tmp_path, monkeypatch):
    """Same rule as the diff context: a bare CALLS target with a unique name."""
    root = (tmp_path / "repo").as_posix()
    repo = _range_store(
        tmp_path, monkeypatch,
        [NodeInfo(kind="Function", name="helper", file_path=f"{root}/util.py",
                  line_start=1, line_end=2, language="python"),
         NodeInfo(kind="Function", name="run", file_path=f"{root}/main.py",
                  line_start=1, line_end=3, language="python")],
        [EdgeInfo(kind="CALLS", source=f"{root}/main.py::run", target="helper",
                  file_path=f"{root}/main.py", line=2)],
    )
    text = enrich_mod.enrich_file_range("util.py", str(repo), 1, 2)
    assert "Called by: run" in text and "none found statically" not in text


def test_signal_receiver_shows_its_sender_and_siblings(tmp_path, monkeypatch):
    from gryphon.django_signal_resolver import resolve_django_signals

    root = (tmp_path / "repo").as_posix()
    repo = _range_store(tmp_path, monkeypatch, [
        NodeInfo(kind="Class", name="Document", file_path=f"{root}/models.py",
                 line_start=1, line_end=9, language="python"),
        NodeInfo(kind="Function", name="capture", file_path=f"{root}/signals.py",
                 line_start=1, line_end=3, language="python",
                 extra={"decorators": ["receiver(pre_save, sender=Document)"]}),
        NodeInfo(kind="Function", name="on_saved", file_path=f"{root}/signals.py",
                 line_start=5, line_end=7, language="python",
                 extra={"decorators": ["receiver(post_save, sender=Document)"]}),
    ])
    store = GraphStore(repo / ".gryphon" / "graph.db")
    try:
        resolve_django_signals(store)
    finally:
        store.close()
    text = enrich_mod.enrich_file_range("signals.py", str(repo), 5, 7)
    assert "Called by: Document (via post_save)" in text
    assert "Other receivers of Document: capture [pre_save]" in text
    assert "none found statically" not in text


def test_search_needs_an_exact_symbol_name(tmp_path, monkeypatch):
    """Half a name or a phrase used to pull in keyword matches."""
    root = (tmp_path / "repo").as_posix()
    repo = _range_store(tmp_path, monkeypatch, [
        NodeInfo(kind="Function", name="parse_file", file_path=f"{root}/parser.py",
                 line_start=1, line_end=2, language="python"),
    ])
    assert 'named "parse_file"' in enrich_mod.enrich_search("parse_file", str(repo))
    assert enrich_mod.enrich_search("parse", str(repo)) == ""
    assert enrich_mod.enrich_search("Parse_File", str(repo)) == ""


def test_search_skips_a_name_defined_too_often(tmp_path, monkeypatch):
    root = (tmp_path / "repo").as_posix()
    repo = _range_store(tmp_path, monkeypatch, [
        NodeInfo(kind="Function", name="save", file_path=f"{root}/m{i}.py",
                 line_start=1, line_end=2, language="python")
        for i in range(4)
    ])
    assert enrich_mod.enrich_search("save", str(repo)) == ""


def test_whole_file_read_does_not_repeat_symbols_in_a_session(tmp_path, monkeypatch):
    root = (tmp_path / "repo").as_posix()
    repo = _range_store(tmp_path, monkeypatch, [
        NodeInfo(kind="Function", name="helper", file_path=f"{root}/util.py",
                 line_start=1, line_end=2, language="python"),
    ])
    seen = enrich_mod._SessionMemory(str(repo), "sess")
    assert "helper" in enrich_mod.enrich_file_range("util.py", str(repo), None, None, seen=seen)
    assert enrich_mod.enrich_file_range("util.py", str(repo), None, None, seen=seen) == ""


def test_no_static_caller_says_what_may_call_it(tmp_path, monkeypatch):
    root = (tmp_path / "repo").as_posix()
    repo = _range_store(tmp_path, monkeypatch, [
        NodeInfo(kind="Function", name="sync_job", file_path=f"{root}/tasks.py",
                 line_start=1, line_end=2, language="python",
                 extra={"decorators": ["shared_task(bind=True)"]}),
    ])
    text = enrich_mod.enrich_file_range("tasks.py", str(repo), 1, 2)
    assert "none found statically" in text and "; decorated @shared_task" in text

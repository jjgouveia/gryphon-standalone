"""Tests for the A/B review benchmark harness (gryphon/eval/review_ab)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from gryphon.eval.review_ab import runner, sandbox
from gryphon.eval.review_ab.cases import ReviewCase, load_cases, save_cases, upsert_case
from gryphon.eval.review_ab.prompts import build_prompt
from gryphon.eval.review_ab.runner import (
    RunSettings,
    audit_tool_calls,
    build_command,
    parse_stream,
    run_arm,
    summarize,
)
from gryphon.eval.review_ab.sandbox import ArmSandbox, changed_files, prepare_arm


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> dict:
    """base -> head (the PR) -> future fix on main, plus agent config files."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("def total(xs):\n    return sum(xs)\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("Use pytest.\n", encoding="utf-8")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text('{"hooks": {}}', encoding="utf-8")
    (repo / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app.py").write_text(
        "def total(xs):\n    return sum(xs) + 1\n", encoding="utf-8",
    )
    _git(repo, "commit", "-q", "-am", "pr change")
    head = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "feature", "-m", "merge pr")
    (repo / "app.py").write_text("def total(xs):\n    return sum(xs)\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "fix: off by one")
    future = _git(repo, "rev-parse", "HEAD")
    return {"path": repo, "base": base, "head": head, "future": future}


@pytest.fixture
def case(source_repo: dict) -> ReviewCase:
    return ReviewCase(
        id="demo-1",
        source_repo=str(source_repo["path"]),
        base_sha=source_repo["base"],
        head_sha=source_repo["head"],
        title="Ajusta total",
        body="Soma um ao total.",
    )


# --- cases ------------------------------------------------------------------


def test_case_rejects_short_sha():
    with pytest.raises(ValueError, match="40-char"):
        ReviewCase(id="x", source_repo=".", base_sha="abc", head_sha="a" * 40, title="t")


def test_case_rejects_unsafe_id():
    with pytest.raises(ValueError, match="invalid case id"):
        ReviewCase(id="../x", source_repo=".", base_sha="a" * 40, head_sha="b" * 40, title="t")


def test_cases_roundtrip_and_upsert(tmp_path: Path, case: ReviewCase):
    path = tmp_path / "cases.yaml"
    save_cases([case], path)
    assert load_cases(path) == [case]

    renamed = ReviewCase(**{**case.__dict__, "title": "Novo título"})
    upsert_case(renamed, path)
    loaded = load_cases(path)
    assert len(loaded) == 1 and loaded[0].title == "Novo título"


def test_load_cases_rejects_duplicate_ids(tmp_path: Path, case: ReviewCase):
    path = tmp_path / "cases.yaml"
    save_cases([case, case], path)
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(path)


# --- sandbox ----------------------------------------------------------------


def test_sandbox_hides_future_and_agent_config(tmp_path: Path, case, source_repo):
    sb = prepare_arm(case, "baseline", tmp_path / "work")
    repo = sb.repo

    missing = subprocess.run(
        ["git", "cat-file", "-e", source_repo["future"]], cwd=str(repo), capture_output=True,
    )
    assert missing.returncode != 0, "the fix commit after the PR must not be in the clone"
    assert _git(repo, "rev-parse", "HEAD") == source_repo["head"]
    assert not (repo / ".claude").exists()
    assert not (repo / ".mcp.json").exists()
    assert (repo / "AGENTS.md").exists()
    assert not (repo / ".git" / "FETCH_HEAD").exists()
    assert _git(repo, "status", "--porcelain") == ""
    assert sb.contamination_warnings == []
    assert [f["path"] for f in changed_files(sb)] == ["app.py"]
    # Opaque directory name: neither the arm nor the case id shows up.
    assert "baseline" not in sb.root.name and "demo" not in sb.root.name


def test_sandbox_is_reused_until_fresh(tmp_path: Path, case):
    first = prepare_arm(case, "baseline", tmp_path / "work")
    (first.repo / "scratch.txt").write_text("x", encoding="utf-8")
    again = prepare_arm(case, "baseline", tmp_path / "work")
    assert (again.repo / "scratch.txt").exists()
    fresh = prepare_arm(case, "baseline", tmp_path / "work", fresh=True)
    assert not (fresh.repo / "scratch.txt").exists()


def test_contamination_warning_for_graph_mention(tmp_path: Path, source_repo):
    repo = source_repo["path"]
    _git(repo, "checkout", "-q", "feature")
    (repo / "AGENTS.md").write_text("Use the gryphon MCP tools first.\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "mention graph")
    head = _git(repo, "rev-parse", "HEAD")
    c = ReviewCase(
        id="demo-2", source_repo=str(repo), base_sha=source_repo["base"],
        head_sha=head, title="t",
    )
    sb = prepare_arm(c, "baseline", tmp_path / "work")
    assert sb.contamination_warnings == ["AGENTS.md mentions the graph tool"]


def test_graph_arm_keeps_graph_outside_clone(tmp_path: Path, case, monkeypatch):
    built = {}

    def fake_build(sb: ArmSandbox, timeout: int) -> None:
        built["env"] = sb.gryphon_env()
        sb.graph_build_seconds = 1.5

    monkeypatch.setattr(sandbox, "_build_graph", fake_build)
    sb = prepare_arm(case, "graph", tmp_path / "work")
    assert sb.data_dir is not None and sb.crg_home is not None
    assert sb.repo not in sb.data_dir.parents and sb.repo not in sb.crg_home.parents
    assert built["env"] == {"CRG_DATA_DIR": str(sb.data_dir), "CRG_HOME": str(sb.crg_home)}

    monkeypatch.setattr(sandbox, "_build_graph", lambda *a: pytest.fail("rebuilt"))
    reused = prepare_arm(case, "graph", tmp_path / "work")
    assert reused.graph_build_seconds == 1.5


def test_unknown_arm_rejected(tmp_path: Path, case):
    with pytest.raises(ValueError, match="unknown arm"):
        prepare_arm(case, "oracle", tmp_path / "work")


# --- prompt -----------------------------------------------------------------


def test_prompts_differ_only_in_tools_section():
    files = [{"path": "app.py", "added": 1, "deleted": 1}]
    base = build_prompt("baseline", title="T", body="B", files=files)
    graph = build_prompt("graph", title="T", body="B", files=files)
    assert "gryphon" not in base.lower()
    assert "detect_changes_tool" in graph
    for text in (base, graph):
        assert "- app.py (+1/-1)" in text
        assert "git diff base...review" in text
    head_a, _, tail_a = base.partition("How to explore:")
    head_b, _, tail_b = graph.partition("How to explore:")
    assert head_a == head_b
    assert tail_a.split("Return the review")[1] == tail_b.split("Return the review")[1]


# --- command ----------------------------------------------------------------


def _sb(tmp_path: Path, kind: str) -> ArmSandbox:
    root = tmp_path / kind
    sb = ArmSandbox(case_id="c", kind=kind, root=root, repo=root / "repo")
    if kind == "graph":
        sb.data_dir, sb.crg_home = root / ".data", root / ".home"
    return sb


def _allowed(cmd: list[str]) -> list[str]:
    # --allowedTools is variadic and last on the command line.
    return cmd[cmd.index("--allowedTools") + 1:]


def test_baseline_command_has_no_graph(tmp_path: Path):
    cmd = build_command(_sb(tmp_path, "baseline"), RunSettings())
    assert "--restricted" in cmd and "--strict-mcp-config" in cmd
    assert json.loads(cmd[cmd.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert "--settings" not in cmd and "--append-system-prompt" not in cmd
    assert "Bash(gh *)" in cmd and "WebFetch" in cmd
    assert "Bash(git *)" in _allowed(cmd)
    assert not any("gryphon" in a for a in _allowed(cmd))
    assert cmd[cmd.index("--model") + 1] == runner.DEFAULT_MODEL


def test_graph_command_reproduces_the_install(tmp_path: Path):
    sb = _sb(tmp_path, "graph")
    cmd = build_command(sb, RunSettings(model="claude-haiku-4-5", effort="medium"))
    server = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]["gryphon"]
    assert server["args"][-2:] == ["--repo", str(sb.repo)]
    assert server["env"]["CRG_DATA_DIR"] == str(sb.data_dir)
    assert "mcp__gryphon__*" in _allowed(cmd)
    hook = json.loads(cmd[cmd.index("--settings") + 1])["hooks"]["SessionStart"][0]
    assert "-m gryphon status" in hook["hooks"][0]["command"]
    assert "## MCP Tools: gryphon" in cmd[cmd.index("--append-system-prompt") + 1]
    assert cmd[cmd.index("--effort") + 1] == "medium"


def test_child_env_isolates_gryphon_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CRG_DATA_DIR", "/user/graph")
    assert "CRG_DATA_DIR" not in runner._child_env(_sb(tmp_path, "baseline"))
    graph = _sb(tmp_path, "graph")
    env = runner._child_env(graph)
    assert "CLAUDECODE" not in env
    assert env["CRG_DATA_DIR"] == str(graph.data_dir)


# --- stream parsing and audit -------------------------------------------------


def _stream(findings: list[dict] | None = None, tool_calls=(), **result) -> list[str]:
    events = [
        {"type": "system", "subtype": "init", "tools": ["Bash", "Read"], "mcp_servers": []},
        {
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": name, "input": inp} for name, inp in tool_calls
            ]},
        },
        {
            "type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
            "duration_ms": 1000, "total_cost_usd": 0.12,
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 500, "output_tokens": 40},
            "structured_output": (
                None if findings is None else {"summary": "ok", "findings": findings}
            ),
            **result,
        },
    ]
    return [json.dumps(e) for e in events] + ["not json", ""]


def test_parse_and_summarize_ok():
    finding = {"file": "app.py", "line": 2, "severity": "major", "category": "bug",
               "claim": "off by one", "evidence": "+ 1"}
    parsed = parse_stream(_stream(
        [finding],
        tool_calls=[("Bash", {"command": "git diff"}), ("mcp__gryphon__query_graph_tool", {})],
    ))
    rec = summarize(parsed, exit_code=0, timed_out=False)
    assert rec["status"] == "ok"
    assert rec["findings"] == [finding]
    assert rec["total_cost_usd"] == 0.12 and rec["cache_read_input_tokens"] == 500
    assert rec["tool_calls_total"] == 2 and rec["graph_tool_calls"] == 1


def test_summarize_status_without_structured_output():
    parsed = parse_stream(_stream(None))
    assert summarize(parsed, exit_code=0, timed_out=False)["status"] == "no_structured_output"
    assert summarize(parse_stream([]), exit_code=1, timed_out=False)["status"] == "no_result"
    assert summarize(parse_stream([]), exit_code=None, timed_out=True)["status"] == "timeout"


def test_audit_flags_network_and_escapes(tmp_path: Path):
    sb = _sb(tmp_path, "baseline")
    source = str(tmp_path / "source")
    calls = [
        {"name": "Bash", "input": {"command": "git diff base...review"}},
        {"name": "Bash", "input": {"command": f"cat {sb.repo}/app.py"}},
        {"name": "Grep", "input": {"pattern": "x", "path": "src"}},
        {"name": "Bash", "input": {"command": "gh pr view 3"}},
        {"name": "Bash", "input": {"command": "git fetch origin"}},
        {"name": "Bash", "input": {"command": f"git -C {source} log"}},
        {"name": "Bash", "input": {"command": "ls ../"}},
        {"name": "Read", "input": {"file_path": str(tmp_path / "elsewhere.py")}},
        {"name": "Read", "input": {
            "file_path": str(Path.home() / ".claude" / "projects" / "x" / "tool-results" / "a.txt"),
        }},
    ]
    leaks, notes = audit_tool_calls(calls, sb, source)

    def indices(flags):
        return sorted({int(f.split()[0][1:]) for f in flags})

    assert indices(leaks) == [3, 4, 5, 7]
    assert indices(notes) == [6]


# --- run_arm ----------------------------------------------------------------


def test_run_arm_records_a_fake_review(tmp_path: Path, case, monkeypatch):
    sb = prepare_arm(case, "baseline", tmp_path / "work")
    finding = {"file": "app.py", "line": 2, "severity": "major", "category": "bug",
               "claim": "soma 1 a mais", "evidence": "return sum(xs) + 1"}
    seen = {}

    def fake_run(cmd, *, cwd, input, stdout, **kwargs):
        seen.update(cmd=cmd, cwd=cwd, prompt=input, env=kwargs["env"])
        stdout.write("\n".join(_stream(
            [finding], tool_calls=[("Bash", {"command": "git diff base...review"})],
        )))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(runner, "_run_claude", fake_run)
    monkeypatch.setenv("CLAUDECODE", "1")
    run_dir = tmp_path / "run"
    rec = run_arm(case, sb, RunSettings(), arm="baseline", rep=0, run_dir=run_dir)

    assert seen["cwd"] == str(sb.repo)
    assert "Ajusta total" in seen["prompt"] and "- app.py (+1/-1)" in seen["prompt"]
    assert "CLAUDECODE" not in seen["env"]
    assert rec["status"] == "ok" and rec["findings"] == [finding]
    assert rec["leak_flags"] == [] and rec["changed_files"] == 1
    assert (run_dir / rec["stream"]).exists()
    assert (run_dir / "prompts" / "demo-1__baseline.txt").exists()


def test_run_arm_keeps_partial_transcript_on_timeout(tmp_path: Path, case, monkeypatch):
    sb = prepare_arm(case, "baseline", tmp_path / "work")

    def slow_run(cmd, *, stdout, timeout, **kwargs):
        stdout.write(json.dumps({"type": "system", "subtype": "init", "tools": []}) + "\n")
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(runner, "_run_claude", slow_run)
    rec = run_arm(
        case, sb, RunSettings(timeout_s=5), arm="baseline", rep=0, run_dir=tmp_path / "run",
    )
    assert rec["status"] == "timeout" and rec["exit_code"] is None


# --- graph_required ---------------------------------------------------------


def test_graph_arms_share_one_sandbox(tmp_path: Path, case, monkeypatch):
    builds = []
    monkeypatch.setattr(sandbox, "_build_graph", lambda sb, timeout: builds.append(sb.root))
    graph = prepare_arm(case, "graph", tmp_path / "work")
    required = prepare_arm(case, "graph_required", tmp_path / "work")
    assert graph.root == required.root and graph.kind == required.kind == "graph"
    assert len(builds) == 1


def test_graph_required_prompt_adds_the_protocol():
    files = [{"path": "app.py", "added": 1, "deleted": 1}]
    graph = build_prompt("graph", title="T", body="B", files=files)
    required = build_prompt("graph_required", title="T", body="B", files=files)
    assert "Required protocol" in required and "Required protocol" not in graph
    assert required.startswith(graph.split("Return the review")[0].rstrip())


def test_run_arm_rejects_arm_in_wrong_sandbox(tmp_path: Path, case):
    sb = prepare_arm(case, "baseline", tmp_path / "work")
    with pytest.raises(ValueError, match="cannot run"):
        run_arm(case, sb, RunSettings(), arm="graph", rep=0, run_dir=tmp_path / "run")


@pytest.mark.parametrize(
    ("calls", "ok"),
    [
        (["mcp__gryphon__get_minimal_context_tool", "mcp__gryphon__detect_changes_tool"], True),
        (["Bash", "mcp__gryphon__get_minimal_context_tool"], False),
    ],
)
def test_graph_required_records_protocol(tmp_path, case, monkeypatch, calls, ok):
    monkeypatch.setattr(sandbox, "_build_graph", lambda sb, timeout: None)
    sb = prepare_arm(case, "graph_required", tmp_path / "work")

    def fake_run(cmd, *, stdout, **kwargs):
        stdout.write("\n".join(_stream([], tool_calls=[(c, {}) for c in calls])))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(runner, "_run_claude", fake_run)
    rec = run_arm(case, sb, RunSettings(), arm="graph_required", rep=0, run_dir=tmp_path / "r")
    assert rec["protocol_ok"] is ok
    assert rec["first_tools"] == calls


# --- judge ------------------------------------------------------------------

from gryphon.eval.review_ab import judge as judge_mod  # noqa: E402
from gryphon.eval.review_ab.score import aggregate_by_arm, render_markdown, score_case  # noqa: E402


def _finding(file="app.py", claim="soma 1 a mais", **kw):
    return {"file": file, "line": 2, "severity": "major", "category": "bug",
            "claim": claim, "evidence": "return sum(xs) + 1", **kw}


def _record(arm, rep, findings, stream=None, **kw):
    return {"case_id": "demo-1", "arm": arm, "rep": rep, "status": "ok", "leak_flags": [],
            "summary": "s", "findings": findings, "total_cost_usd": 0.4, "num_turns": 10,
            "wall_seconds": 90.0, "graph_tool_calls": 3 if arm != "baseline" else 0,
            "stream": stream or f"streams/demo-1__{arm}__r{rep}.jsonl", **kw}


def test_labels_are_a_deterministic_shuffle():
    first = judge_mod._labels("demo-1", "run-a", 6)
    assert first == judge_mod._labels("demo-1", "run-a", 6)
    assert sorted(first) == [f"R{i}" for i in range(1, 7)]
    assert first != judge_mod._labels("demo-1", "run-b", 6) or first != sorted(first)


def test_scrub_hides_arm_tells():
    text = "O mcp__gryphon__query_graph_tool mostrou via gryphon e detect_changes_tool."
    assert judge_mod.scrub(text) == "O [tool] mostrou via [tool] e [tool]."


def test_judgeable_skips_failed_and_leaking_runs():
    records = [_record("baseline", 0, []), _record("graph", 0, [], status="timeout"),
               _record("graph", 1, [], leak_flags=["#1 network"])]
    assert [r["arm"] for r in judge_mod.judgeable(records, "demo-1")] == ["baseline"]


def test_judge_prompt_does_not_reveal_arms(case):
    labeled = [("R1", _record("graph_required", 0, [_finding(claim="via gryphon")])),
               ("R2", _record("baseline", 0, []))]
    prompt = judge_mod.build_judge_prompt(case, [{"path": "app.py", "added": 1, "deleted": 1}],
                                          labeled)
    lowered = prompt.lower()
    for tell in ("baseline", "graph_required", "gryphon", "arm"):
        assert not re.search(rf"\b{tell}\b", lowered), tell
    assert "F0. [major/bug] app.py:2" in prompt and "Findings: none." in prompt


def test_check_assignment_reports_gaps():
    labeled = [("R1", {"findings": [{}, {}]}), ("R2", {"findings": [{}]})]
    judgment = {"issues": [
        {"reported_by": [{"review": "R1", "finding": 0}, {"review": "R2", "finding": 0}]},
        {"reported_by": [{"review": "R1", "finding": 0}, {"review": "R3", "finding": 0}]},
    ]}
    assert judge_mod.check_assignment(judgment, labeled) == {
        "unassigned": ["R1:F1"], "duplicated": ["R1:F0"], "unknown": ["R3:F0"],
    }


def test_judge_case_writes_blind_inputs_and_judgment(tmp_path, case, monkeypatch):
    run_dir = tmp_path / "runs" / "run-a"
    run_dir.mkdir(parents=True)
    records = [_record("baseline", 0, [_finding()]), _record("graph", 0, [])]
    (run_dir / "records.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records), encoding="utf-8",
    )
    verdict = {"issues": [], "reviews": []}
    seen = {}

    def fake_run(cmd, *, stdout, input, **kwargs):
        seen["cmd"], seen["prompt"] = cmd, input
        stdout.write(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                 "total_cost_usd": 1.5, "structured_output": verdict}))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(judge_mod, "_run_claude", fake_run)
    settings = judge_mod.judge_settings(RunSettings())
    path = judge_mod.judge_case(case, run_dir, settings, workdir=tmp_path / "work")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "ok" and payload["judgment"] == verdict
    assert payload["assignment"]["unassigned"]  # the judge above assigned nothing
    labels = json.loads((run_dir / "judgments" / "demo-1.labels.json").read_text())
    assert sorted(m["arm"] for m in labels.values()) == ["baseline", "graph"]
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == judge_mod.JUDGE_MODEL
    assert "mcp__gryphon__*" not in seen["cmd"]
    assert json.loads(seen["cmd"][seen["cmd"].index("--json-schema") + 1]) == judge_mod.JUDGE_SCHEMA
    assert "graph" not in seen["prompt"].lower()


# --- score ------------------------------------------------------------------


def _judged():
    labels = {
        "R1": {"arm": "baseline", "rep": 0, "stream": "s/b0"},
        "R2": {"arm": "graph_required", "rep": 0, "stream": "s/g0"},
    }
    records = [
        _record("baseline", 0, [_finding(), _finding(claim="falso")], stream="s/b0"),
        _record("graph_required", 0, [_finding(), _finding(claim="outro")], stream="s/g0"),
    ]
    scores = {k: 4 for k in judge_mod.RUBRIC}
    judgment = {
        "issues": [
            {"id": "I1", "title": "off by one", "file": "app.py", "verdict": "real",
             "severity": "major", "rationale": "r", "known_issue": "K1",
             "reported_by": [{"review": "R1", "finding": 0}, {"review": "R2", "finding": 0}]},
            {"id": "I2", "title": "falso", "file": "app.py", "verdict": "false",
             "severity": "none", "rationale": "r", "known_issue": None,
             "reported_by": [{"review": "R1", "finding": 1}]},
            {"id": "I3", "title": "outro", "file": "app.py", "verdict": "real",
             "severity": "minor", "rationale": "r", "known_issue": None,
             "reported_by": [{"review": "R2", "finding": 1}]},
        ],
        "reviews": [{"review": "R1", "scores": scores, "comment": "c1"},
                    {"review": "R2", "scores": {**scores, "impact": 5}, "comment": "c2"}],
    }
    payload = {"case_id": "demo-1", "judgment": judgment}
    return payload, labels, records


def test_score_case_metrics():
    payload, labels, records = _judged()
    scored = score_case(payload, labels, records, known_ids=["K1", "K2"])
    base, graph = scored["rows"]
    assert (base["real"], base["false"], base["precision"]) == (1, 1, 0.5)
    assert base["pooled_recall"] == 0.5 and graph["pooled_recall"] == 1.0
    assert base["weighted_recall"] == round(2 / 3, 3) and graph["weighted_recall"] == 1.0
    assert base["known_recall"] == 0.5
    assert graph["impact"] == 5
    matrix = {m["id"]: m["found_by"] for m in scored["matrix"]}
    assert matrix["I1"] == {"baseline": 1, "graph_required": 1}
    assert matrix["I3"] == {"graph_required": 1}


def test_aggregate_and_render():
    payload, labels, records = _judged()
    scored_case = score_case(payload, labels, records, known_ids=[])
    by_arm = aggregate_by_arm(scored_case["rows"])
    assert list(by_arm) == ["baseline", "graph_required"]
    assert by_arm["graph_required"]["pooled_recall"] == 1.0
    md = render_markdown({
        "run": {"run_id": "run-a", "model": "m", "effort": "high", "prompt_version": "3",
                "reps": 1, "cases": ["demo-1"]},
        "records": records, "judges": [{"status": "ok", "judge_model": "j",
                                         "judge_version": "1", "total_cost_usd": 1.0}],
        "cases": {"demo-1": scored_case}, "rows": scored_case["rows"], "by_arm": by_arm,
    })
    assert "| graph_required | 1 |" in md
    assert "| I1 | real | major | `app.py` | 1 | 1 | off by one |" in md


# --- mine (SZZ) ---------------------------------------------------------------

from gryphon.eval.review_ab.mine import mine_case  # noqa: E402


def test_mine_blames_the_later_fix_on_the_pr(case, source_repo):
    candidates = mine_case(case)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["fix_commit"] == source_repo["future"]
    assert cand["fix_subject"] == "fix: off by one"
    assert cand["blamed_lines"][0]["file"] == "app.py"
    assert cand["blamed_lines"][0]["introduced_by"] == [source_repo["head"][:12]]
    assert cand["confirmed"] is False


def test_mine_ignores_fixes_of_code_the_pr_did_not_write(case, source_repo):
    repo = source_repo["path"]
    (repo / "AGENTS.md").write_text("Use pytest -q.\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "fix: docs typo")
    subjects = [c["fix_subject"] for c in mine_case(case)]
    assert "fix: docs typo" not in subjects


def test_candidates_reach_the_judge_with_their_fix_diff(case, source_repo):
    """Unconfirmed SZZ candidates go to the judge tagged, with the later diff."""
    with_candidate = ReviewCase(**{**case.__dict__, "known_issues": [
        {"id": "K-1", "description": "fix: off by one", "confirmed": False, "file": "app.py",
         "fix_commit": source_repo["future"],
         "blamed_lines": [{"file": "app.py", "lines": "2-2"}]},
        {"id": "K-2", "description": "manual"},
    ]})
    assert [k["id"] for k in with_candidate.confirmed_issues()] == ["K-2"]
    prompt = judge_mod.build_judge_prompt(with_candidate, [], [])
    assert "- K-1 [CANDIDATE] app.py" in prompt and "- K-2 [CONFIRMED]" in prompt
    assert "-    return sum(xs) + 1" in prompt  # the fix's diff, as reference
    assert "known_verdicts" in prompt


def test_judge_confirmed_candidates_count_as_ground_truth():
    payload, labels, records = _judged()
    payload["judgment"]["known_verdicts"] = [
        {"id": "K1", "is_defect": True, "rationale": "fixes the off by one"},
        {"id": "K9", "is_defect": False, "rationale": "rename only"},
    ]
    scored = score_case(payload, labels, records, known_ids=[])
    assert scored["known_ids"] == ["K1"] and scored["judge_confirmed"] == ["K1"]
    base, graph = scored["rows"]
    assert base["known_recall"] == 1.0 and graph["known_recall"] == 1.0


def test_load_runs_pools_several_runs(tmp_path):
    from gryphon.eval.review_ab.score import load_runs

    payload, labels, records = _judged()
    for run_id in ("run-a", "run-b"):
        d = tmp_path / run_id
        (d / "judgments").mkdir(parents=True)
        (d / "run.json").write_text(json.dumps({
            "run_id": run_id, "model": "m", "effort": "high", "prompt_version": "3",
            "reps": 1, "cases": ["demo-1"],
        }), encoding="utf-8")
        (d / "records.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records), encoding="utf-8")
        (d / "judgments" / "demo-1.json").write_text(
            json.dumps({**payload, "status": "ok", "judge_model": "j", "judge_version": "2",
                        "total_cost_usd": 0.5}), encoding="utf-8")
        (d / "judgments" / "demo-1.labels.json").write_text(json.dumps(labels), encoding="utf-8")
    pooled = load_runs([tmp_path / "run-a", tmp_path / "run-b"], {})
    assert list(pooled["cases"]) == ["demo-1", "demo-1 (run-b)"]
    assert pooled["by_arm"]["baseline"]["reviews"] == 2
    assert pooled["run"]["run_id"] == "run-a + run-b" and pooled["run"]["model"] == "m"
    assert "Recall gabarito" in render_markdown(pooled)


def test_audit_ignores_drive_lookalikes_inside_grep_patterns(tmp_path: Path):
    sb = _sb(tmp_path, "baseline")
    cmd = r'grep -n "if user_id:\|def x" src/*.py; echo C:\Windows'
    leaks, _ = audit_tool_calls([{"name": "Bash", "input": {"command": cmd}}], sb, "/src")
    assert leaks == [r"#0 absolute path outside the clone: C:\Windows"]


def test_reaudit_run_rewrites_flags_from_streams(tmp_path: Path, case):
    run_dir = tmp_path / "run"
    (run_dir / "streams").mkdir(parents=True)
    stream = "streams/demo-1__baseline__r0.jsonl"
    (run_dir / stream).write_text("\n".join(_stream(
        [], tool_calls=[("Bash", {"command": r'grep "user_id:\|y" a.py'})],
    )), encoding="utf-8")
    rec = _record("baseline", 0, [], stream=stream, leak_flags=["#0 stale"], audit_notes=[])
    (run_dir / "records.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    assert runner.reaudit_run(run_dir, [case], tmp_path / "work") == 1
    updated = json.loads((run_dir / "records.jsonl").read_text(encoding="utf-8"))
    assert updated["leak_flags"] == []


# --- adoption ablation: graph_md, graph_md_enrich, isolation ------------------


def test_project_isolation_loads_project_settings_only(tmp_path: Path):
    cmd = build_command(_sb(tmp_path, "baseline"), RunSettings(isolation="project"))
    assert "--restricted" not in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    with pytest.raises(ValueError, match="unknown isolation"):
        build_command(_sb(tmp_path, "baseline"), RunSettings(isolation="nope"))


def test_graph_md_arms_skip_the_appended_prompt(tmp_path: Path):
    sb = _sb(tmp_path, "graph")
    settings = RunSettings(isolation="project")
    graph = build_command(sb, settings, arm="graph")
    md = build_command(sb, settings, arm="graph_md")
    enrich = build_command(sb, settings, arm="graph_md_enrich")
    assert "--append-system-prompt" in graph
    assert "--append-system-prompt" not in md and "--append-system-prompt" not in enrich

    def hooks(cmd):
        return json.loads(cmd[cmd.index("--settings") + 1])["hooks"]

    assert "PreToolUse" not in hooks(md)
    pre = hooks(enrich)["PreToolUse"][0]
    assert pre["matcher"] == "Grep|Glob|Bash|Read"
    assert pre["hooks"][0]["command"].endswith("-m gryphon enrich")


def test_graph_md_needs_project_isolation(tmp_path, case, monkeypatch):
    monkeypatch.setattr(sandbox, "_build_graph", lambda sb, timeout: None)
    sb = prepare_arm(case, "graph_md", tmp_path / "work")
    with pytest.raises(ValueError, match="needs isolation"):
        run_arm(case, sb, RunSettings(), arm="graph_md", rep=0, run_dir=tmp_path / "r")


def test_graph_md_writes_claude_md_only_during_the_run(tmp_path, case, monkeypatch):
    monkeypatch.setattr(sandbox, "_build_graph", lambda sb, timeout: None)
    sb = prepare_arm(case, "graph_md", tmp_path / "work")
    seen = {}

    def fake_run(cmd, *, stdout, cwd, **kwargs):
        seen["claude_md"] = (Path(cwd) / "CLAUDE.md").read_text(encoding="utf-8")
        stdout.write("\n".join(_stream([])))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(runner, "_run_claude", fake_run)
    rec = run_arm(case, sb, RunSettings(isolation="project"), arm="graph_md", rep=0,
                  run_dir=tmp_path / "r")
    assert "## MCP Tools: gryphon" in seen["claude_md"]
    assert not (sb.repo / "CLAUDE.md").exists()
    assert _git(sb.repo, "status", "--porcelain") == ""
    assert rec["isolation"] == "project"


def test_graph_md_restores_a_tracked_claude_md(tmp_path, source_repo, monkeypatch):
    repo = source_repo["path"]
    _git(repo, "checkout", "-q", "feature")
    (repo / "CLAUDE.md").write_text("# Regras do repo\n", encoding="utf-8")
    _git(repo, "add", "CLAUDE.md")
    _git(repo, "commit", "-q", "-m", "claude md")
    c = ReviewCase(id="demo-3", source_repo=str(repo), base_sha=source_repo["base"],
                   head_sha=_git(repo, "rev-parse", "HEAD"), title="t")
    monkeypatch.setattr(sandbox, "_build_graph", lambda sb, timeout: None)
    sb = prepare_arm(c, "graph_md_enrich", tmp_path / "work")
    seen = {}

    def fake_run(cmd, *, stdout, cwd, **kwargs):
        seen["claude_md"] = (Path(cwd) / "CLAUDE.md").read_text(encoding="utf-8")
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(runner, "_run_claude", fake_run)
    run_arm(c, sb, RunSettings(isolation="project"), arm="graph_md_enrich", rep=0,
            run_dir=tmp_path / "r")
    assert seen["claude_md"].startswith("# Regras do repo")
    assert "## MCP Tools: gryphon" in seen["claude_md"]
    # Restored even though the run timed out.
    assert (sb.repo / "CLAUDE.md").read_text(encoding="utf-8") == "# Regras do repo\n"
    assert _git(sb.repo, "status", "--porcelain") == ""


def test_graph_install_uses_the_install_verbatim(tmp_path: Path):
    from gryphon.skills import generate_hooks_config

    sb = _sb(tmp_path, "graph")
    cmd = build_command(sb, RunSettings(isolation="project"), arm="graph_install")
    assert json.loads(cmd[cmd.index("--settings") + 1]) == generate_hooks_config(sb.repo)
    assert "--append-system-prompt" not in cmd
    env = runner._child_env(sb)
    assert env["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)


def test_usage_limit_is_reported_and_stops_the_run(tmp_path, case, monkeypatch):
    """A 429 (session limit) would fail every later review: stop at the first."""
    limited = json.dumps({
        "type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
        "api_error_status": 429, "terminal_reason": "api_error", "total_cost_usd": 0,
        "result": "You've hit your session limit",
    })
    calls = []

    def fake_run(cmd, *, stdout, **kwargs):
        calls.append(cmd)
        stdout.write(limited)
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(runner, "_run_claude", fake_run)
    with pytest.raises(runner.RunAbortedError, match="api_error:429"):
        runner.run_cases([case], arms=("baseline",), reps=3, out_dir=tmp_path / "out",
                         workdir=tmp_path / "work")
    assert len(calls) == 1
    rec = json.loads(next((tmp_path / "out").glob("*/records.jsonl")).read_text())
    assert rec["status"] == "api_error:429"
    assert rec["result_text"] == "You've hit your session limit"


def test_other_errors_name_the_terminal_reason():
    parsed = parse_stream([json.dumps({"type": "result", "subtype": "error_max_turns",
                                       "is_error": True, "terminal_reason": "max_turns"})])
    assert summarize(parsed, exit_code=1, timed_out=False)["status"] == "error:max_turns"


def test_every_arm_reaches_the_summary():
    """Regression: a hard-coded arm list dropped graph_install from the report."""
    from gryphon.eval.review_ab.sandbox import ARMS

    rows = [{"arm": arm, "findings": 1} for arm in ARMS]
    assert list(aggregate_by_arm(rows)) == list(ARMS)


def test_building_the_cli_does_not_import_the_runner():
    """Every `gryphon` call (hooks included) builds the parser: keep it light."""
    code = (
        "import sys, argparse\n"
        "from gryphon.eval.review_ab.cli import add_parser\n"
        "add_parser(argparse.ArgumentParser().add_subparsers())\n"
        "print('gryphon.eval.review_ab.runner' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_mine_repo_credits_the_pr_that_introduced_the_fixed_lines(tmp_path: Path):
    """PR #1 introduces a bug, PR #2 fixes it: #1 is ranked with the fix, #2 is not."""
    from gryphon.eval.review_ab.mine import mine_repo

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("def total(xs):\n    return sum(xs)\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "app.py").write_text("def total(xs):\n    return sum(xs) + 1\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "feat: new total")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "feat", "-m", "Merge pull request #1 from o/feat")

    _git(repo, "checkout", "-q", "-b", "fix")
    (repo / "app.py").write_text("def total(xs):\n    return sum(xs)\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "fix: off by one in total")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "fix", "-m", "Merge pull request #2 from o/fix")

    ranked = mine_repo(repo, ["main"])
    assert [r["pr"] for r in ranked] == [1]
    issue = ranked[0]["known_issues"][0]
    assert issue["fix_pr"] == 2 and issue["fix_subject"] == "fix: off by one in total"
    assert issue["file"] == "app.py" and issue["confirmed"] is False
    assert ranked[0]["changed_commits"] == 1


def test_removed_ranges_survives_a_commit_without_parent(tmp_path: Path):
    """The root commit (or a shallow clone's edge) has no parent to diff."""
    from gryphon.eval.review_ab.mine import removed_ranges

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix: root")
    assert removed_ranges(repo, _git(repo, "rev-parse", "HEAD")) == {}


def test_mine_repo_credits_the_feature_pr_not_the_promotion(tmp_path: Path):
    """feature -> homologation -> main: the bug belongs to the small feature PR."""
    from gryphon.eval.review_ab.mine import mine_repo

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("def total(xs):\n    return sum(xs)\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "homologation")
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "app.py").write_text("def total(xs):\n    return sum(xs) + 1\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "feat: new total")
    _git(repo, "checkout", "-q", "homologation")
    _git(repo, "merge", "-q", "--no-ff", "feat", "-m", "Merge pull request #7 from o/feat")
    (repo / "b.py").write_text("y = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: other work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "homologation", "-m",
         "Merge pull request #9 from o/homologation")
    (repo / "app.py").write_text("def total(xs):\n    return sum(xs)\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "fix: off by one")

    ranked = mine_repo(repo, ["main"])
    assert [r["pr"] for r in ranked] == [7]


def test_mine_repo_keeps_only_the_hosts_prs(tmp_path: Path):
    """Merges from another repository's history are dropped; numbers come from the host."""
    from gryphon.eval.review_ab.mine import merged_prs, mine_repo

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("def total(xs):\n    return sum(xs)\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "app.py").write_text("def total(xs):\n    return sum(xs) + 1\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "feat: new total")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "feat", "-m", "Merge pull request #2515 from old/feat")
    (repo / "app.py").write_text("def total(xs):\n    return sum(xs)\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "fix: off by one")
    merge = merged_prs(repo, ["main"])[0]["merge_sha"]

    assert mine_repo(repo, ["main"], merge_shas={}) == []
    ranked = mine_repo(repo, ["main"], merge_shas={merge: 12})
    assert [r["pr"] for r in ranked] == [12]


def test_mine_repo_by_sha_accepts_any_message_and_squash_merges(tmp_path: Path):
    """Hosts can title merges with the PR title, or squash them into one commit."""
    from gryphon.eval.review_ab.mine import merged_prs, mine_repo

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (repo / "b.py").write_text("def b():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    # PR 1: a merge commit titled like the PR, not "Merge pull request".
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "a.py").write_text("def a():\n    return 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "feat: a")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "feat", "-m", "feat: a (#1)")
    merge_1 = _git(repo, "rev-parse", "HEAD")
    # PR 2: squash merge, a single-parent commit.
    (repo / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "feat: b (#2)")
    squash_2 = _git(repo, "rev-parse", "HEAD")
    # Later fixes of both.
    (repo / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (repo / "b.py").write_text("def b():\n    return 1\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "fix: revert a and b")

    host = {merge_1: 1, squash_2: 2, "0" * 40: 3}  # PR 3 is not in this clone
    prs = {p["pr"]: p for p in merged_prs(repo, ["main"], merge_shas=host)}
    assert set(prs) == {1, 2}
    assert prs[2]["commits"] == {squash_2} and prs[2]["head_sha"] == squash_2
    assert sorted(r["pr"] for r in mine_repo(repo, ["main"], merge_shas=host)) == [1, 2]


def test_case_without_merge_base_is_skipped_with_a_clear_error(tmp_path, monkeypatch):
    """Base and head from unrelated histories (a shallow source): skip, keep going."""
    repo = tmp_path / "src"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "a")
    (repo / "x.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "--orphan", "b")
    (repo / "x.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "b")
    head = _git(repo, "rev-parse", "HEAD")
    orphan = ReviewCase(id="orphan-1", source_repo=str(repo), base_sha=base, head_sha=head,
                        title="t")
    with pytest.raises(sandbox.NoMergeBaseError, match="deepen"):
        prepare_arm(orphan, "baseline", tmp_path / "work")
    calls = []
    monkeypatch.setattr(runner, "_run_claude", lambda *a, **k: calls.append(a))
    runner.run_cases([orphan], arms=("baseline",), out_dir=tmp_path / "out",
                     workdir=tmp_path / "work")
    assert calls == []


# --- round 2: reference install, pooled baseline, paired report, jobs --------


def _judged_two_baselines():
    """Two baseline reviews that each find one of two real issues."""
    labels = {
        "R1": {"arm": "baseline", "rep": 0, "stream": "s/b0"},
        "R2": {"arm": "baseline", "rep": 1, "stream": "s/b1"},
        "R3": {"arm": "graph_install", "rep": 0, "stream": "s/g0"},
    }
    records = [
        _record("baseline", 0, [_finding(), _finding(claim="falso")], stream="s/b0"),
        _record("baseline", 1, [_finding(claim="outro")], stream="s/b1"),
        _record("graph_install", 0, [_finding()], stream="s/g0"),
    ]
    scores = {k: 4 for k in judge_mod.RUBRIC}
    judgment = {
        "issues": [
            {"id": "I1", "title": "off by one", "file": "app.py", "verdict": "real",
             "severity": "major", "rationale": "r", "known_issue": "K1",
             "reported_by": [{"review": "R1", "finding": 0}, {"review": "R3", "finding": 0}]},
            {"id": "I2", "title": "falso", "file": "app.py", "verdict": "false",
             "severity": "none", "rationale": "r", "known_issue": None,
             "reported_by": [{"review": "R1", "finding": 1}]},
            {"id": "I3", "title": "outro", "file": "app.py", "verdict": "real",
             "severity": "minor", "rationale": "r", "known_issue": None,
             "reported_by": [{"review": "R2", "finding": 0}]},
        ],
        "reviews": [{"review": r, "scores": scores, "comment": ""} for r in labels],
    }
    return {"case_id": "demo-1", "judgment": judgment}, labels, records


def test_pooled_baseline_unions_two_reviews():
    payload, labels, records = _judged_two_baselines()
    scored = score_case(payload, labels, records, known_ids=["K1"])
    pooled = [r for r in scored["rows"] if r["arm"] == "baseline_x2"]
    assert len(pooled) == 1
    row = pooled[0]
    assert (row["findings"], row["real"], row["false"]) == (3, 2, 1)
    assert row["pooled_recall"] == 1.0 and row["known_recall"] == 1.0
    assert row["cost_usd"] == 0.8 and row["label"] == "R1+R2"
    assert row["correctness"] is None  # the judge never scored a pooled review
    assert "baseline_x2" not in scored["reps_per_arm"]


def test_paired_deltas_compare_case_means():
    from gryphon.eval.review_ab.score import paired_deltas

    payload, labels, records = _judged_two_baselines()
    case = score_case(payload, labels, records, known_ids=[])
    deltas = paired_deltas({"demo-1": case, "demo-2": case}, "graph_install")
    # graph: 1 real; baseline mean (1 + 1) / 2 = 1.
    assert deltas["real"] == {"n": 2, "mean": 0.0, "ci": (0.0, 0.0), "better": 0, "worse": 0}
    assert deltas["pooled_recall"]["mean"] == 0.0
    assert paired_deltas({"demo-1": case}, "baseline_x2")["pooled_recall"]["mean"] == 0.5


def test_report_has_paired_section_per_repository():
    from gryphon.eval.review_ab.score import render_paired

    payload, labels, records = _judged_two_baselines()
    case = score_case(payload, labels, records, known_ids=[])
    text = "\n".join(render_paired({"cases": {"api-1": case, "front-2": case}}))
    assert "### graph_install − baseline (todos os casos)" in text
    assert "### graph_install − baseline (api)" in text
    assert "### baseline_x2 − baseline (front)" in text


def test_reference_install_comes_from_the_reference_interpreter(tmp_path, monkeypatch):
    """graph_install_ref takes hooks, MCP server and PATH from --ref-python."""
    sb = _sb(tmp_path, "graph")
    ref = str(tmp_path / "ref" / "Scripts" / "python.exe")
    seen = []

    def fake_ref_call(python, code, *args):
        seen.append((python, args))
        return json.dumps({"hooks": {"SessionStart": []}})

    monkeypatch.setattr(runner, "_ref_call", fake_ref_call)
    settings = RunSettings(isolation="project", ref_python=ref)
    cmd = build_command(sb, settings, arm="graph_install_ref")
    assert json.loads(cmd[cmd.index("--settings") + 1]) == {"hooks": {"SessionStart": []}}
    assert seen == [(ref, (str(sb.repo),))]
    mcp = json.loads(cmd[cmd.index("--mcp-config") + 1])
    assert mcp["mcpServers"]["gryphon"]["command"] == ref
    env = runner._child_env(sb, ref)
    assert env["PATH"].split(os.pathsep)[0] == str(Path(ref).parent)
    with pytest.raises(ValueError, match="--ref-python"):
        build_command(sb, RunSettings(isolation="project"), arm="graph_install_ref")


def test_jobs_run_cases_in_parallel_and_still_stop_at_a_limit(tmp_path, case, monkeypatch):
    import threading

    second = ReviewCase(**{**case.__dict__, "id": "demo-2"})
    ok = "\n".join(_stream([]))
    active, peak = [0], [0]
    lock = threading.Lock()
    release = threading.Barrier(2, timeout=10)

    def fake_run(cmd, *, stdout, **kwargs):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        release.wait()  # both cases must be in flight at once
        with lock:
            active[0] -= 1
        stdout.write(ok)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(runner, "_run_claude", fake_run)
    run_dir = runner.run_cases([case, second], arms=("baseline",), reps=1,
                               out_dir=tmp_path / "out", workdir=tmp_path / "work", jobs=2)
    assert peak[0] == 2
    lines = (run_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()
    assert sorted(json.loads(x)["case_id"] for x in lines) == ["demo-1", "demo-2"]


def test_reference_calls_do_not_import_the_checkout_they_run_from(tmp_path, monkeypatch):
    """``python -c`` from this checkout imported this gryphon, not the reference one."""
    monkeypatch.chdir(Path(runner.__file__).parents[3])
    out = runner._ref_call(sys.executable, "import sys; print(repr(sys.path[0]))")
    assert out.strip() not in ("''", repr(str(Path.cwd())))


# --- merge stage ----------------------------------------------------------------

from gryphon.eval.review_ab import merge as merge_mod  # noqa: E402


def _merged_finding(claim="soma 1 a mais", check="verified", reported_by="both", **kw):
    return {**_finding(claim=claim), "reported_by": reported_by, "check": check,
            "check_note": "n", **kw}


def _source_run(tmp_path: Path, case_id: str = "demo-1", reps=(0, 1)) -> Path:
    run = tmp_path / "src-run"
    (run / "streams").mkdir(parents=True, exist_ok=True)
    records = []
    for rep in reps:
        rec = _record("baseline", rep, [_finding(claim=f"achado {rep}")])
        rec["case_id"] = case_id
        rec["stream"] = f"streams/{case_id}__baseline__r{rep}.jsonl"
        (run / rec["stream"]).write_text("{}", encoding="utf-8")
        records.append(rec)
    (run / "records.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return run


def test_merge_prompt_carries_both_reviews_and_the_right_checks(case):
    sources = [_record("baseline", 0, [_finding(claim="um")]),
               _record("baseline", 1, [_finding(claim="dois")])]
    files = [{"path": "app.py", "added": 1, "deleted": 1}]
    plain = merge_mod.build_merge_prompt("merge", case, files, sources)
    graph = merge_mod.build_merge_prompt("merge_graph", case, files, sources)
    for prompt in (plain, graph):
        assert "### Reviewer A" in prompt and "A0. [major/bug] app.py:2 — um" in prompt
        assert "### Reviewer B" in prompt and "B0. [major/bug] app.py:2 — dois" in prompt
        assert "Do not drop a finding because only one" in prompt
    assert "query_graph_tool" not in plain and "grep the repository" in plain
    assert 'pattern="callers_of"' in graph and "The graph is static" in graph
    with pytest.raises(ValueError, match="unknown variant"):
        merge_mod.build_merge_prompt("other", case, files, sources)


def test_contradicted_findings_leave_the_final_list_but_are_kept():
    merged = [_merged_finding("um"), _merged_finding("dois", check="contradicted"),
              _merged_finding("tres", check="not_verified", reported_by="A")]
    final, demoted = merge_mod.split_findings(merged)
    assert [f["claim"] for f in final] == ["um", "tres"]
    assert [f["claim"] for f in demoted] == ["dois"]
    assert set(final[0]) == {"file", "line", "severity", "category", "claim", "evidence"}


def test_sources_need_two_usable_baseline_reviews(tmp_path):
    run = _source_run(tmp_path)
    found = merge_mod.find_sources([run], "demo-1")
    assert found and [r["rep"] for r in found[1]] == [0, 1]
    assert merge_mod.find_sources([run], "other-case") is None
    assert merge_mod.find_sources([_source_run(tmp_path / "x", reps=(0,))], "demo-1") is None


def _fake_merge_run(calls, findings, **result):
    def fake_run(cmd, *, stdout, **kwargs):
        calls.append(cmd)
        stdout.write("\n".join(_stream(findings, **result)))
        return subprocess.CompletedProcess(cmd, 0)
    return fake_run


def test_run_merge_stores_the_final_list_and_the_demoted_ones(tmp_path, case, monkeypatch):
    monkeypatch.setattr(sandbox, "_build_graph", lambda sb, timeout: None)
    calls: list = []
    monkeypatch.setattr(merge_mod, "_run_claude", _fake_merge_run(calls, [
        _merged_finding("fica"), _merged_finding("cai", check="contradicted")]))
    sources = merge_mod.find_sources([_source_run(tmp_path)], case.id)[1]
    records = merge_mod.run_merge(case, "merge_graph", sources, RunSettings(),
                                  workdir=tmp_path / "work", run_dir=tmp_path / "out")
    final, demoted = records
    assert final["arm"] == "merge_graph" and [f["claim"] for f in final["findings"]] == ["fica"]
    assert final["checks"] == {"verified": 1, "contradicted": 1, "not_verified": 0}
    assert demoted["arm"] == "merge_graph_demoted"
    assert [f["claim"] for f in demoted["findings"]] == ["cai"]
    assert demoted["total_cost_usd"] == 0  # the cost is counted once, on the final record
    # The graph variant runs in the graph sandbox with the MCP server; the plain one does not.
    mcp = json.loads(calls[0][calls[0].index("--mcp-config") + 1])
    assert "gryphon" in mcp["mcpServers"]


def test_run_merge_without_graph_has_no_mcp_server(tmp_path, case, monkeypatch):
    calls: list = []
    monkeypatch.setattr(merge_mod, "_run_claude", _fake_merge_run(calls, []))
    sources = merge_mod.find_sources([_source_run(tmp_path)], case.id)[1]
    records = merge_mod.run_merge(case, "merge", sources, RunSettings(),
                                  workdir=tmp_path / "work", run_dir=tmp_path / "out")
    assert len(records) == 1 and records[0]["findings"] == []
    mcp = json.loads(calls[0][calls[0].index("--mcp-config") + 1])
    assert mcp["mcpServers"] == {}


def test_merge_run_copies_the_sources_and_resumes_without_repeating(tmp_path, case, monkeypatch):
    calls: list = []
    monkeypatch.setattr(merge_mod, "_run_claude", _fake_merge_run(calls, [_merged_finding()]))
    src = _source_run(tmp_path, case.id)
    kwargs = dict(variants=("merge",), settings=RunSettings(), out_dir=tmp_path / "out",
                  workdir=tmp_path / "work")
    run_dir = merge_mod.run_merge_cases([case], [src], **kwargs)
    records = [json.loads(x) for x in (run_dir / "records.jsonl").read_text().splitlines()]
    assert sorted(r["arm"] for r in records) == ["baseline", "baseline", "merge"]
    assert (run_dir / records[0]["stream"]).exists()  # the transcript came along
    assert len(calls) == 1
    merge_mod.run_merge_cases([case], [src], resume=run_dir, **kwargs)
    assert len(calls) == 1  # nothing left to do
    assert len((run_dir / "records.jsonl").read_text().splitlines()) == 3


def test_merge_run_stops_at_a_usage_limit_and_resumes(tmp_path, case, monkeypatch):
    limited = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                          "num_turns": 1, "api_error_status": 429,
                          "terminal_reason": "api_error", "total_cost_usd": 0,
                          "result": "session limit"})

    def fake_run(cmd, *, stdout, **kwargs):
        stdout.write(limited)
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(merge_mod, "_run_claude", fake_run)
    src = _source_run(tmp_path, case.id)
    kwargs = dict(variants=("merge",), settings=RunSettings(), out_dir=tmp_path / "out",
                  workdir=tmp_path / "work")
    with pytest.raises(runner.RunAbortedError, match="api_error:429"):
        merge_mod.run_merge_cases([case], [src], **kwargs)
    run_dir = next((tmp_path / "out").iterdir())
    calls: list = []
    monkeypatch.setattr(merge_mod, "_run_claude", _fake_merge_run(calls, [_merged_finding()]))
    merge_mod.run_merge_cases([case], [src], resume=run_dir, **kwargs)
    assert len(calls) == 1
    arms = [json.loads(x)["arm"] for x in (run_dir / "records.jsonl").read_text().splitlines()]
    assert arms.count("baseline") == 2 and arms.count("merge") == 2  # failed + ok


def test_top3_serious_counts_real_major_findings_among_the_first_three():
    payload, labels, records = _judged()
    scored = score_case(payload, labels, records, known_ids=[])
    rows = {r["arm"]: r for r in scored["rows"]}
    # baseline: F0 real major, F1 false. graph_required: F0 real major, F1 real minor.
    assert rows["baseline"]["top3_serious"] == 1
    assert rows["graph_required"]["top3_serious"] == 1


def test_report_compares_the_merge_arms_with_the_pooled_baseline():
    from gryphon.eval.review_ab.score import ARM_ORDER, render_paired

    assert ARM_ORDER[-4:] == ("merge", "merge_demoted", "merge_graph", "merge_graph_demoted")
    payload, labels, records = _judged_two_baselines()
    labels["R4"] = {"arm": "merge", "rep": 0, "stream": "s/m"}
    labels["R5"] = {"arm": "merge_graph", "rep": 0, "stream": "s/mg"}
    records += [_record("merge", 0, [_finding()], stream="s/m"),
                _record("merge_graph", 0, [_finding()], stream="s/mg")]
    payload["judgment"]["reviews"] += [
        {"review": r, "scores": {k: 4 for k in judge_mod.RUBRIC}, "comment": ""}
        for r in ("R4", "R5")]
    payload["judgment"]["issues"][0]["reported_by"] += [
        {"review": "R4", "finding": 0}, {"review": "R5", "finding": 0}]
    case_scored = score_case(payload, labels, records, known_ids=[])
    text = "\n".join(render_paired({"cases": {"api-1": case_scored}}))
    assert "### merge − baseline_x2 (todos os casos)" in text
    assert "### merge_graph − baseline_x2 (todos os casos)" in text
    assert "### merge_graph − merge (todos os casos)" in text
    assert "demoted" not in text

"""Transitive impact benchmark: the functions affected, up to two call levels, by a signature change.

Ground truth: the call graph resolved by the TypeScript checker (callgraph.js). Arms: baseline
(grep and file reads) and graph (the gryphon tools, protocol required). No judge.

  python evaluate/impact_ab/run_depth.py run [--reps 2] [--jobs 3] [--resume DIR]
  python evaluate/impact_ab/run_depth.py report DIR
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run as base  # noqa: E402

from gryphon.eval.review_ab.runner import (  # noqa: E402
    STOP_STATUSES,
    RunSettings,
    _child_env,
    _run_claude,
    audit_tool_calls,
    build_command,
    parse_stream,
    summarize,
)

HERE = Path(__file__).parent

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "functions"],
    "properties": {
        "summary": {"type": "string"},
        "functions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file", "function", "level"],
                "properties": {
                    "file": {"type": "string"},
                    "function": {"type": "string"},
                    "level": {"enum": [1, 2]},
                },
            },
        },
    },
}

TASK = """\
You are working in the Git repository in the current directory, a TypeScript/React
application.

We are going to change the signature of the function `{name}`, declared at
`{file}:{line}`. List the functions that this change reaches, in two levels:
- level 1: every function that calls `{name}`, or renders it as a JSX component;
- level 2: every function that calls, or renders, a level-1 function.
Look in all tracked .ts and .tsx files, production code and tests.

Rules:
- A function is a named function declaration, a method, or a const bound to an arrow or
  function expression. Report an anonymous callback under the named function that contains
  it. Ignore top-level module code.
- Check which declaration each call resolves to, through imports, aliases and re-exports:
  do not list callers of other functions that share a name.
- A function that is both level 1 and level 2 is level 1. Do not list `{name}` itself.
- Do not modify, create or delete files. There is no network access.

{tools}

Return the result through the structured output: a short `summary` and `functions`, one
`{{file, function, level}}` entry per function, with `file` relative to the repository root and
`function` as declared.
"""

TOOLS = {
    "baseline": """\
How to explore: use grep and file reads. For level 2, repeat the search for each level-1
function. Check imports to know which declaration each match refers to.""",
    "graph": """\
How to explore: a code knowledge graph of this repository is available through the
gryphon MCP tools. Start with it, then verify in the source.
1. `query_graph_tool(pattern="callers_of", target=<name or qualified name>,
   detail_level="minimal")` lists the callers of a function; repeat it for each level-1
   function. If the name is ambiguous, pick the declaration at the file above.
2. `get_impact_radius_tool` gives the blast radius of a file in one call; use it to cross-check.
3. The graph can be incomplete (an empty answer means "not statically visible"): grep the
   name as well and read the matches you doubt.""",
}


def score(target: dict, reported: list[dict]) -> dict:
    truth1 = {(m["file"], m["name"]) for m in target["level1"]}
    truth2 = {(m["file"], m["name"]) for m in target["level2"]}
    truth = truth1 | truth2
    got = {(base.norm(r["file"]), r["function"].split(".")[-1]) for r in reported}
    tp = len(got & truth)
    precision = tp / len(got) if got else 0.0
    recall = tp / len(truth) if truth else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "reported": len(got), "true": len(truth), "tp": tp,
        "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3),
        "level1_recall": round(len(got & truth1) / len(truth1), 3) if truth1 else 0.0,
        "level2_recall": round(len(got & truth2) / len(truth2), 3) if truth2 else 0.0,
    }


def run_one(target: dict, arm: str, rep: int, settings: RunSettings, run_dir: Path) -> dict:
    sb = base.sandbox(arm)
    prompt = TASK.format(name=target["name"], file=target["file"], line=target["line"],
                         tools=TOOLS[arm])
    stem = f"{target['id']}__{arm}__r{rep}"
    (run_dir / "streams").mkdir(parents=True, exist_ok=True)
    stream_path = run_dir / "streams" / f"{stem}.jsonl"
    started = time.monotonic()
    exit_code, timed_out = None, False
    with open(stream_path, "w", encoding="utf-8") as out, open(
        run_dir / "streams" / f"{stem}.stderr", "w", encoding="utf-8"
    ) as err:
        try:
            proc = _run_claude(
                build_command(sb, settings, arm=arm, schema=SCHEMA),
                cwd=str(sb.repo), input=prompt, stdout=out, stderr=err, text=True,
                encoding="utf-8", errors="replace", env=_child_env(sb), timeout=settings.timeout_s,
            )
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
    with open(stream_path, encoding="utf-8", errors="replace") as f:
        parsed = parse_stream(f)
    leaks, notes = audit_tool_calls(parsed["tool_calls"], sb, base.SOURCE_REPO)
    flat = summarize(parsed, exit_code=exit_code, timed_out=timed_out)
    result = parsed["result"] or {}
    reported = []
    if flat["status"] == "ok" and isinstance(result.get("structured_output"), dict):
        reported = result["structured_output"].get("functions") or []
    rec = {
        "target": target["id"], "name": target["name"], "group": "transitive", "arm": arm,
        "rep": rep, "model": settings.model, "effort": settings.effort,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "wall_seconds": round(time.monotonic() - started, 1), "leak_flags": leaks,
        "audit_notes": notes, "stream": str(stream_path.relative_to(run_dir)),
        "status": flat["status"], "total_cost_usd": flat["total_cost_usd"],
        "num_turns": flat["num_turns"], "tool_calls": flat["tool_calls"],
        "graph_tool_calls": flat["graph_tool_calls"], "result_text": flat["result_text"],
        "items": reported,
    }
    if flat["status"] == "ok":
        rec.update(score(target, reported))
    return rec


def run(reps: int, jobs: int, resume: Path | None, model: str | None = None,
        effort: str | None = None) -> Path:
    targets = json.loads((HERE / "targets_depth.json").read_text(encoding="utf-8"))
    settings = RunSettings(**{k: v for k, v in (('model', model), ('effort', effort)) if v})
    run_dir = resume or HERE / "runs" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-depth" + (f"-{model}" if model else ""))
    run_dir.mkdir(parents=True, exist_ok=True)
    records_path = run_dir / "records.jsonl"
    (run_dir / "run.json").write_text(json.dumps({
        "model": settings.model, "effort": settings.effort, "reps": reps,
        "targets": [t["id"] for t in targets], "arms": ["baseline", "graph"],
    }, indent=2), encoding="utf-8")
    while True:
        done = {(r["target"], r["arm"], r["rep"]) for r in base.read_records(records_path)
                if r["status"] == "ok"}
        work = [(t, arm, rep) for rep in range(reps) for t in targets for arm in ("baseline", "graph")
                if (t["id"], arm, rep) not in done]
        if not work:
            return run_dir
        random.Random(13).shuffle(work)
        lock, stop, limited = threading.Lock(), threading.Event(), []

        def go(item):
            t, arm, rep = item
            if stop.is_set():
                return
            rec = run_one(t, arm, rep, settings, run_dir)
            with lock:
                with open(records_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"{t['id']} {arm} r{rep}: {rec['status']} ${rec['total_cost_usd']} "
                      f"turns={rec['num_turns']} f1={rec.get('f1')} P={rec.get('precision')} "
                      f"R={rec.get('recall')} graph_calls={rec['graph_tool_calls']}", flush=True)
            if rec["status"] in STOP_STATUSES:
                limited.append(rec.get("result_text") or "")
                stop.set()

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(go, work))
        if limited:
            base.wait_for_reset(limited[0])
            continue
        return run_dir


def report(run_dir: Path) -> str:
    recs = [r for r in base.read_records(run_dir / "records.jsonl")
            if r["status"] == "ok" and not r["leak_flags"] and "f1" in r]
    metrics = ("precision", "recall", "level1_recall", "level2_recall", "f1", "total_cost_usd",
               "num_turns", "wall_seconds", "graph_tool_calls")
    lines = [f"# Transitive impact benchmark, run {run_dir.name}", "",
             "| Braço | Execuções | " + " | ".join(metrics) + " |", "|---|---|" + "---|" * len(metrics)]
    for arm in ("baseline", "graph"):
        rows = [r for r in recs if r["arm"] == arm]
        if rows:
            vals = " | ".join(f"{statistics.mean(r[m] or 0 for r in rows):.3f}" for m in metrics)
            lines.append(f"| {arm} | {len(rows)} | {vals} |")
    by_t: dict[str, dict[str, list[dict]]] = {}
    for r in recs:
        by_t.setdefault(r["target"], {}).setdefault(r["arm"], []).append(r)
    lines += ["", f"Diferença pareada por alvo (graph − baseline), {len(by_t)} alvos, IC 95% bootstrap:", "",
              "| Métrica | Diferença | IC 95% | Melhor | Pior |", "|---|---|---|---|---|"]
    for m in metrics[:-1]:
        diffs = [statistics.mean(r[m] or 0 for r in a["graph"]) - statistics.mean(r[m] or 0 for r in a["baseline"])
                 for a in by_t.values() if a.get("graph") and a.get("baseline")]
        if len(diffs) >= 2:
            lo, hi = base.bootstrap(diffs)
            lines.append(f"| {m} | {statistics.mean(diffs):+.3f} | [{lo:+.3f}, {hi:+.3f}] "
                         f"| {sum(d > 0 for d in diffs)} | {sum(d < 0 for d in diffs)} |")
    lines += ["", "## Por alvo", "", "| Alvo | Verdade | Braço | Reportadas | VP | Precisão | Recall | Custo | Turnos |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(recs, key=lambda r: (r["target"], r["arm"], r["rep"])):
        lines.append(f"| {r['target']} | {r['true']} | {r['arm']} r{r['rep']} | {r['reported']} | {r['tp']} "
                     f"| {r['precision']:.2f} | {r['recall']:.2f} | ${r['total_cost_usd']:.2f} | {r['num_turns']} |")
    text = "\n".join(lines) + "\n"
    (run_dir / "report.md").write_text(text, encoding="utf-8")
    return text


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--reps", type=int, default=2)
    r.add_argument("--jobs", type=int, default=3)
    r.add_argument("--model", default=None)
    r.add_argument("--effort", default=None)
    r.add_argument("--resume", default=None)
    rp = sub.add_parser("report")
    rp.add_argument("dir")
    args = ap.parse_args()
    if args.cmd == "run":
        print("run:", run(args.reps, args.jobs, Path(args.resume) if args.resume else None,
                    args.model, args.effort))
    else:
        print(report(Path(args.dir)))

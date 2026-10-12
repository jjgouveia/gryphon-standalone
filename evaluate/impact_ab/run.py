"""Impact benchmark: list every call site of a function whose signature is about to change.

Ground truth: the TypeScript checker (groundtruth.js). Arms: baseline (grep and file reads) and
graph (the gryphon tools, with the protocol required). Scores precision and recall of the call
sites the agent reports; no judge is involved.

  python evaluate/impact_ab/run.py run [--reps 2] [--jobs 3] [--resume DIR]
  python evaluate/impact_ab/run.py report DIR
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from gryphon.eval.review_ab.runner import (
    STOP_STATUSES,
    RunSettings,
    _child_env,
    _run_claude,
    audit_tool_calls,
    build_command,
    parse_stream,
    summarize,
)
from gryphon.eval.review_ab.sandbox import ArmSandbox

HERE = Path(__file__).parent
RAB = Path.home() / "AppData" / "Local" / "Temp" / "rab"
SOURCE_REPO = r"C:\Users\gouvi\Documents\Desenvolvimento\celer\ativos-cvld-front"
TOLERANCE = 2  # lines

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "call_sites"],
    "properties": {
        "summary": {"type": "string"},
        "call_sites": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file", "line"],
                "properties": {"file": {"type": "string"}, "line": {"type": "integer"}},
            },
        },
    },
}

TASK = """\
You are working in the Git repository in the current directory, a TypeScript/React
application.

We are going to change the signature of the function `{name}`, declared at
`{file}:{line}`, by adding a required parameter. List every call site that must be
updated: each place in the tracked .ts and .tsx files (production code and tests) where
this exact function is called.

Rules:
- Do not list the declaration, import lines, or type-only uses. Do not list calls to
  other functions that happen to share the name: check which declaration each call
  resolves to, through imports, aliases and re-exports.
- Report the line of the function name at the call.
- Do not modify, create or delete files. There is no network access.

{tools}

Return the result through the structured output: a short `summary` and `call_sites`,
one `{{file, line}}` entry per call, with `file` relative to the repository root.
"""

TOOLS = {
    "baseline": """\
How to explore: use grep and file reads. Check each match's imports to know which
declaration it refers to.""",
    "graph": """\
How to explore: a code knowledge graph of this repository is available through the
gryphon MCP tools. Start with it, then verify in the source.
1. `query_graph_tool(pattern="callers_of", target=<name or qualified name>,
   detail_level="minimal")` lists the callers. If the name is ambiguous, the answer
   lists the candidates: pick the declaration at the file above.
2. Use `get_impact_radius_tool` only if the callers stay unclear.
3. The graph can be incomplete (an empty answer means "not statically visible"): grep
   the name as well and read the matches you doubt.""",
}


def sandbox(arm: str) -> ArmSandbox:
    name = "b5ec23519869" if arm == "graph" else "c3e1b7285d95"
    root = RAB / name
    sb = ArmSandbox(case_id="impact", kind=arm, root=root, repo=root / "repo")
    if arm == "graph":
        sb.data_dir, sb.crg_home = root / ".data", root / ".home"
    return sb


def norm(path: str) -> str:
    path = path.replace("\\", "/")
    return path[2:] if path.startswith("./") else path


def score(truth: list[dict], reported: list[dict]) -> dict:
    """Match reported call sites to the true ones: same file, line within TOLERANCE."""
    free = [(t["file"], t["line"]) for t in truth]
    tp = 0
    seen: set[tuple[str, int]] = set()
    for r in reported:
        key = (norm(r["file"]), r["line"])
        if key in seen:
            continue
        seen.add(key)
        best = min(
            (i for i, (f, ln) in enumerate(free) if f == key[0] and abs(ln - key[1]) <= TOLERANCE),
            key=lambda i: abs(free[i][1] - key[1]), default=None,
        )
        if best is not None:
            free.pop(best)
            tp += 1
    n_rep, n_true = len(seen), len(truth)
    precision = tp / n_rep if n_rep else 0.0
    recall = tp / n_true if n_true else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    true_files = {t["file"] for t in truth}
    rep_files = {f for f, _ in seen}
    return {
        "reported": n_rep, "true": n_true, "tp": tp,
        "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3),
        "file_recall": round(len(true_files & rep_files) / len(true_files), 3),
        "file_precision": round(len(true_files & rep_files) / len(rep_files), 3) if rep_files else 0.0,
    }


def run_one(target: dict, arm: str, rep: int, settings: RunSettings, run_dir: Path) -> dict:
    sb = sandbox(arm)
    prompt = TASK.format(
        name=target["name"], file=target["file"], line=target["line"], tools=TOOLS[arm],
    )
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
    leaks, notes = audit_tool_calls(parsed["tool_calls"], sb, SOURCE_REPO)
    flat = summarize(parsed, exit_code=exit_code, timed_out=timed_out)
    structured_ok = flat["status"] == "ok"
    reported = []
    result = parsed["result"] or {}
    if structured_ok and isinstance(result.get("structured_output"), dict):
        reported = result["structured_output"].get("call_sites") or []
    rec = {
        "target": target["id"], "name": target["name"], "group": target["group"], "arm": arm,
        "rep": rep, "model": settings.model, "effort": settings.effort,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "wall_seconds": round(time.monotonic() - started, 1), "leak_flags": leaks,
        "audit_notes": notes, "stream": str(stream_path.relative_to(run_dir)),
        "status": flat["status"], "total_cost_usd": flat["total_cost_usd"],
        "num_turns": flat["num_turns"], "tool_calls": flat["tool_calls"],
        "graph_tool_calls": flat["graph_tool_calls"], "result_text": flat["result_text"],
        "items": reported,
    }
    if structured_ok:
        rec.update(score(target["calls"], reported))
    return rec


def wait_for_reset(text: str) -> None:
    brt = dt.timezone(dt.timedelta(hours=-3))
    m = re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.I)
    now = dt.datetime.now(brt)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        target = now.replace(hour=hour, minute=int(m.group(2) or 0), second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        target += dt.timedelta(minutes=2)
    else:
        target = now + dt.timedelta(minutes=10)
    print(f"usage limit: waiting until {target:%H:%M}", flush=True)
    while dt.datetime.now(brt) < target:
        time.sleep(30)


def read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def run(reps: int, jobs: int, resume: Path | None, only: list[str] | None,
        model: str | None = None, effort: str | None = None) -> Path:
    targets = json.loads((HERE / "targets.json").read_text(encoding="utf-8"))
    if only:
        targets = [t for t in targets if t["id"] in only]
    settings = RunSettings(**{k: v for k, v in (('model', model), ('effort', effort)) if v})
    if resume:
        run_dir = resume
    else:
        run_dir = HERE / "runs" / (datetime.now().strftime("%Y%m%d-%H%M%S") + (f"-{model}" if model else ""))
        run_dir.mkdir(parents=True)
    records_path = run_dir / "records.jsonl"
    (run_dir / "run.json").write_text(json.dumps({
        "model": settings.model, "effort": settings.effort, "reps": reps,
        "targets": [t["id"] for t in targets], "arms": ["baseline", "graph"],
    }, indent=2), encoding="utf-8")
    while True:
        done = {(r["target"], r["arm"], r["rep"]) for r in read_records(records_path)
                if r["status"] == "ok"}
        work = []
        for rep in range(reps):
            for t in targets:
                order = ["baseline", "graph"] if (rep + int(t["id"][1:3])) % 2 == 0 else ["graph", "baseline"]
                work += [(t, arm, rep) for arm in order if (t["id"], arm, rep) not in done]
        if not work:
            return run_dir
        random.Random(11).shuffle(work)
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
            wait_for_reset(limited[0])
            continue
        # Anything still missing failed for another reason; one more pass would repeat it.
        return run_dir


def bootstrap(diffs: list[float], seed: int = 7, n: int = 10000) -> tuple[float, float]:
    rng = random.Random(seed)  # nosec B311
    means = sorted(statistics.mean(rng.choices(diffs, k=len(diffs))) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def report(run_dir: Path) -> str:
    recs = [r for r in read_records(run_dir / "records.jsonl")
            if r["status"] == "ok" and not r["leak_flags"] and "f1" in r]
    metrics = ("precision", "recall", "f1", "file_recall", "total_cost_usd", "num_turns", "wall_seconds",
               "graph_tool_calls")
    lines = [f"# Impact benchmark, run {run_dir.name}", ""]
    for group in ("hard", "easy", "all"):
        sub = [r for r in recs if group == "all" or r["group"] == group]
        lines += [f"## {group}", "", "| Braço | Execuções | " + " | ".join(metrics) + " |",
                  "|---|---|" + "---|" * len(metrics)]
        for arm in ("baseline", "graph"):
            rows = [r for r in sub if r["arm"] == arm]
            if rows:
                vals = " | ".join(f"{statistics.mean(r[m] or 0 for r in rows):.3f}" for m in metrics)
                lines.append(f"| {arm} | {len(rows)} | {vals} |")
        # paired by target: mean over reps, then graph - baseline
        by_t: dict[str, dict[str, list[dict]]] = {}
        for r in sub:
            by_t.setdefault(r["target"], {}).setdefault(r["arm"], []).append(r)
        lines += ["", f"Diferença pareada por alvo (graph − baseline), {len(by_t)} alvos, IC 95% bootstrap:", "",
                  "| Métrica | Diferença | IC 95% | Melhor | Pior |", "|---|---|---|---|---|"]
        for m in metrics[:-1]:
            diffs = [statistics.mean(r[m] or 0 for r in a["graph"]) - statistics.mean(r[m] or 0 for r in a["baseline"])
                     for a in by_t.values() if a.get("graph") and a.get("baseline")]
            if len(diffs) >= 2:
                lo, hi = bootstrap(diffs)
                lines.append(f"| {m} | {statistics.mean(diffs):+.3f} | [{lo:+.3f}, {hi:+.3f}] "
                             f"| {sum(d > 0 for d in diffs)} | {sum(d < 0 for d in diffs)} |")
        lines.append("")
    lines += ["## Por alvo", "", "| Alvo | Grupo | Chamadas | Braço | Reportadas | VP | Precisão | Recall | Custo | Turnos |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(recs, key=lambda r: (r["target"], r["arm"], r["rep"])):
        lines.append(f"| {r['target']} | {r['group']} | {r['true']} | {r['arm']} r{r['rep']} | {r['reported']} "
                     f"| {r['tp']} | {r['precision']:.2f} | {r['recall']:.2f} | ${r['total_cost_usd']:.2f} | {r['num_turns']} |")
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
    r.add_argument("--only", nargs="+", default=None)
    rp = sub.add_parser("report")
    rp.add_argument("dir")
    args = ap.parse_args()
    if args.cmd == "run":
        d = run(args.reps, args.jobs, Path(args.resume) if args.resume else None, args.only,
                args.model, args.effort)
        print(f"run: {d}")
    else:
        print(report(Path(args.dir)))
    sys.exit(0)

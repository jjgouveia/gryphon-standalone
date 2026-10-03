"""Merge two finished reviews into one ranked list, with and without the graph.

The review-multi workflow runs two independent reviews, merges them, checks
each finding and ranks the result. The reviews already exist in a benchmark
run (the baseline repetitions), so this stage evaluates only the merge: it
takes the two baseline reviews of a case and produces

- ``merge``: merged and ranked, each finding checked against the source;
- ``merge_graph``: the same, checked with the gryphon tools.

The final list of a variant is the findings the merger did not mark
``contradicted``. Contradicted findings go to a separate ``*_demoted`` record,
so the judge still rules on them and the report can count real defects a
variant demoted by mistake.

Records land in a new run directory next to copies of the two source
reviews, so one blind judgment covers all of them.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .cases import ReviewCase
from .prompts import CATEGORIES, SEVERITIES, _format_files
from .runner import (
    STOP_STATUSES,
    RunAbortedError,
    RunSettings,
    _child_env,
    _run_claude,
    audit_tool_calls,
    build_command,
    parse_stream,
    summarize,
)
from .sandbox import BASE_BRANCH, REVIEW_BRANCH, changed_files, prepare_arm

logger = logging.getLogger(__name__)

VARIANTS = ("merge", "merge_graph")
CHECKS = ("verified", "contradicted", "not_verified")
SOURCES = ("A", "B", "both")

MERGE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "findings"],
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "file", "line", "severity", "category", "claim", "evidence",
                    "reported_by", "check", "check_note",
                ],
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": ["integer", "null"]},
                    "severity": {"enum": list(SEVERITIES)},
                    "category": {"enum": list(CATEGORIES)},
                    "claim": {"type": "string"},
                    "evidence": {"type": "string"},
                    "reported_by": {"enum": list(SOURCES)},
                    "check": {"enum": list(CHECKS)},
                    "check_note": {"type": "string"},
                },
            },
        },
    },
}

_TASK = """\
You are merging two independent reviews of one pull request, in the Git
repository in the current directory.

The PR branch `{review}` is checked out. It merges into `{base}`; the change
under review is `git diff {base}...{review}`.

Title: {title}

Description:
{body}

Changed files (added/deleted lines):
{files}

Two reviewers worked separately. Their findings:

{reviews}

Your job, in order:

1. Merge. Two findings are the same when they describe the same defect in the
   same code, not merely the same line. Keep one entry per defect and set
   `reported_by` to A, B or both. Do not drop a finding because only one
   reviewer reported it.
2. Check each finding, as described below, and set `check`:
   - `verified`: the check supports the claim;
   - `contradicted`: the check shows the claim is wrong, and `check_note`
     says what showed it (the call site, the test, the line);
   - `not_verified`: you could not check it.
   Never mark a finding `contradicted` on a hunch. If in doubt, use
   `not_verified`. Put what you found in `check_note`.
3. Rank the findings: severity first (blocker, major, minor), then reported
   by both reviewers, then the number of places that call the changed code
   (more first). Return them in that order.

{checks}

Rules:
- Do not add findings of your own. Do not modify, create or delete files, and
  do not commit.
- There is no network access.
- Keep each finding's `claim` and `evidence` as the reviewers wrote them,
  merging the wording when two reviewers described the same defect.

Return the merged list through the structured output: a short `summary` and
one entry per merged finding. Write `summary`, `claim`, `evidence` and
`check_note` in the language of the PR title.
"""

_CHECKS_PLAIN = """\
How to check: use git, grep and file reads in the source.
- "nothing calls X" or "X is dead": grep the repository for X, including
  tests, registrations and string references.
- "caller Y breaks": read Y and the changed code, check the call is real.
- "X has no test": grep the tests for X and read what they assert.
- Read the pre-change code with `git show {base}:<path>` before accepting a
  claim that something changed.
About two lookups per finding, ten in total."""

_CHECKS_GRAPH = """\
How to check: a code knowledge graph of this repository, built at
`{review}`, is available through the gryphon MCP tools. Check by what the
finding claims, then confirm in the source when the graph is silent.
- "nothing calls X" or "X is dead": `query_graph_tool(pattern="callers_of",
  target=<X>, detail_level="minimal")`. Callers found contradict the claim.
- "caller Y breaks": `callers_of` the changed symbol. If Y is not a caller,
  the claim is not verified.
- "X has no test": `query_graph_tool(pattern="tests_for", target=<X>,
  detail_level="minimal")`, then read the test it names. A test that covers
  X contradicts the claim.
- "the change affects other code": `review_diff_tool(base="{base}")` lists
  the changed symbols called from outside the diff.
Use the graph for every finding the table covers; about two calls per
finding, ten in total. The graph is static: callers reached through
signals, decorators and dynamic dispatch are missing, so an empty
`callers_of` does not prove X is unused. Grep the name before you accept
"nothing calls X". The source wins when the two disagree."""


def format_review(label: str, record: dict) -> str:
    lines = [f"### Reviewer {label}", "", f"Summary: {record.get('summary') or '(none)'}", ""]
    findings = record.get("findings") or []
    if not findings:
        lines.append("Findings: none.")
    for i, f in enumerate(findings):
        where = f"{f['file']}:{f['line']}" if f.get("line") is not None else f["file"]
        lines.append(f"{label}{i}. [{f['severity']}/{f['category']}] {where} — {f['claim']}")
        lines.append(f"    Evidence: {f['evidence']}")
    return "\n".join(lines)


def build_merge_prompt(
    variant: str, case: ReviewCase, files: list[dict], sources: list[dict],
) -> str:
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}; expected one of {VARIANTS}")
    fmt = {"base": BASE_BRANCH, "review": REVIEW_BRANCH}
    checks = (_CHECKS_GRAPH if variant == "merge_graph" else _CHECKS_PLAIN).format(**fmt)
    return _TASK.format(
        title=case.title.strip() or "(no title)",
        body=case.body.strip() or "(no description)",
        files=_format_files(files),
        reviews="\n\n".join(format_review(label, rec) for label, rec in zip("AB", sources)),
        checks=checks,
        **fmt,
    )


def _read_records(run_dir: Path) -> list[dict]:
    path = run_dir / "records.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def find_sources(run_dirs: list[Path], case_id: str) -> tuple[Path, list[dict]] | None:
    """The run holding two usable baseline reviews of *case_id*, and those reviews."""
    for run_dir in run_dirs:
        reviews = sorted(
            (
                r for r in _read_records(run_dir)
                if r["case_id"] == case_id and r["arm"] == "baseline"
                and r["status"] == "ok" and not r["leak_flags"]
            ),
            key=lambda r: r["rep"],
        )
        if len(reviews) >= 2:
            return run_dir, reviews[:2]
    return None


def split_findings(merged: list[dict]) -> tuple[list[dict], list[dict]]:
    """``(final, demoted)``: the ranked findings without the contradicted ones,
    and the contradicted ones, in the base schema the judge reads."""
    base_keys = ("file", "line", "severity", "category", "claim", "evidence")
    final, demoted = [], []
    for f in merged:
        row = {k: f[k] for k in base_keys}
        (demoted if f.get("check") == "contradicted" else final).append(row)
    return final, demoted


def run_merge(
    case: ReviewCase, variant: str, sources: list[dict], settings: RunSettings,
    *, workdir: Path, run_dir: Path,
) -> list[dict]:
    """Run one merge step; returns the records to store (final, then demoted)."""
    arm = "graph" if variant == "merge_graph" else "baseline"
    sandbox = prepare_arm(case, arm, workdir)
    files = changed_files(sandbox)
    prompt = build_merge_prompt(variant, case, files, sources)
    stem = f"{case.id}__{variant}__r0"
    (run_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (run_dir / "streams").mkdir(parents=True, exist_ok=True)
    (run_dir / "prompts" / f"{case.id}__{variant}.txt").write_text(prompt, encoding="utf-8")
    stream_path = run_dir / "streams" / f"{stem}.jsonl"
    stderr_path = run_dir / "streams" / f"{stem}.stderr"

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started = time.monotonic()
    exit_code: int | None = None
    timed_out = False
    with open(stream_path, "w", encoding="utf-8") as out, open(
        stderr_path, "w", encoding="utf-8"
    ) as err:
        try:
            proc = _run_claude(
                build_command(sandbox, settings, arm=arm, schema=MERGE_SCHEMA),
                cwd=str(sandbox.repo), input=prompt, stdout=out, stderr=err,
                text=True, encoding="utf-8", errors="replace",
                env=_child_env(sandbox), timeout=settings.timeout_s,
            )
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            logger.warning("%s timed out after %ss", stem, settings.timeout_s)

    with open(stream_path, encoding="utf-8", errors="replace") as f:
        parsed = parse_stream(f)
    leaks, notes = audit_tool_calls(parsed["tool_calls"], sandbox, case.source_repo)
    flat = summarize(parsed, exit_code=exit_code, timed_out=timed_out)
    merged = flat.get("findings") or []
    final, demoted = split_findings(merged)
    checks = [f.get("check") for f in merged]
    base = {
        "case_id": case.id, "rep": 0, "model": settings.model, "effort": settings.effort,
        "isolation": settings.isolation, "started_at": started_at,
        "wall_seconds": round(time.monotonic() - started, 1),
        "leak_flags": leaks, "audit_notes": notes,
        "stream": str(stream_path.relative_to(run_dir)),
        "merged_total": len(merged),
        "checks": {c: checks.count(c) for c in CHECKS},
        "reported_by": {s: [f.get("reported_by") for f in merged].count(s) for s in SOURCES},
        "source_streams": [s["stream"] for s in sources],
    }
    record = {**base, "arm": variant, **flat, "findings": final if flat["status"] == "ok" else None}
    records = [record]
    if flat["status"] == "ok" and demoted:
        records.append({
            # Same transcript, its own key: score.py finds a review's record by
            # its stream, and a shared key made this record replace the final one.
            **base, "arm": f"{variant}_demoted", **flat, "findings": demoted,
            "stream": f"{base['stream']}#demoted",
            "summary": "Findings the merge step marked contradicted.",
            "total_cost_usd": 0, "num_turns": 0, "graph_tool_calls": 0,
        })
    return records


def run_merge_cases(
    cases: list[ReviewCase],
    source_runs: list[Path],
    *,
    variants: tuple[str, ...] = VARIANTS,
    settings: RunSettings = RunSettings(),
    out_dir: Path,
    workdir: Path,
    jobs: int = 1,
    resume: Path | None = None,
) -> Path:
    """Merge the two baseline reviews of every case, once per variant.

    Writes a run directory holding the two source reviews of each case
    (copied, with their transcripts) and one record per variant. With
    *resume*, finished merges of that run are kept and only the missing ones
    run, so a usage limit costs nothing already paid for.
    """
    for variant in variants:
        if variant not in VARIANTS:
            raise ValueError(f"unknown variant {variant!r}; expected one of {VARIANTS}")
    if resume:
        run_dir = resume
        run_id = run_dir.name
    else:
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-merge"
        run_dir = out_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
    records_path = run_dir / "records.jsonl"
    existing = _read_records(run_dir)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": run_id,
        "cases": [c.id for c in cases],
        "arms": ["baseline", *variants],
        "reps": 2,
        "model": settings.model,
        "effort": settings.effort,
        "isolation": settings.isolation,
        "max_budget_usd": settings.max_budget_usd,
        "prompt_version": "merge-1",
        "source_runs": [str(p) for p in source_runs],
    }, indent=2), encoding="utf-8")

    lock = threading.Lock()
    stop = threading.Event()
    aborted: list[str] = []

    def append(records: list[dict]) -> None:
        with lock, open(records_path, "a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def run_case(case: ReviewCase) -> None:
        found = find_sources(source_runs, case.id)
        if found is None:
            print(f"{case.id}: skipped (no two usable baseline reviews)")
            return
        source_dir, sources = found
        done = {r["arm"] for r in existing if r["case_id"] == case.id and r["status"] == "ok"}
        if "baseline" not in done:
            (run_dir / "streams").mkdir(parents=True, exist_ok=True)
            for rec in sources:
                src = source_dir / rec["stream"]
                if src.exists():
                    (run_dir / rec["stream"]).write_bytes(src.read_bytes())
            append(sources)
        for variant in variants:
            if stop.is_set():
                return
            if variant in done:
                continue
            records = run_merge(
                case, variant, sources, settings, workdir=workdir, run_dir=run_dir,
            )
            append(records)
            rec = records[0]
            with lock:
                print(
                    f"{case.id} {variant}: {rec['status']} ${rec['total_cost_usd']} "
                    f"turns={rec['num_turns']} merged={rec['merged_total']} "
                    f"checks={rec['checks']} final={len(rec['findings'] or [])} "
                    f"leaks={len(rec['leak_flags'])}", flush=True,
                )
            if rec["status"] in STOP_STATUSES:
                aborted.append(
                    f"{case.id}/{variant} hit {rec['status']}"
                    f" ({rec.get('result_text') or 'no message'})"
                )
                stop.set()
                return

    if jobs <= 1:
        for case in cases:
            run_case(case)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for future in [pool.submit(run_case, c) for c in cases]:
                future.result()
    if aborted:
        raise RunAbortedError(f"{aborted[0]}; run stopped, records so far in {records_path}")
    return run_dir

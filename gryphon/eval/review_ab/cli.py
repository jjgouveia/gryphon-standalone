"""``gryphon review-eval``: A/B review benchmark commands."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

DEFAULT_CASES = Path("evaluate/review_ab/cases.yaml")
DEFAULT_OUT = Path("evaluate/review_ab/runs")


def add_parser(sub) -> argparse.ArgumentParser:
    """Register ``review-eval`` and its subcommands on *sub*."""
    from .defaults import DEFAULT_BUDGET_USD, DEFAULT_EFFORT, DEFAULT_MODEL, DEFAULT_TIMEOUT_S

    cmd = sub.add_parser(
        "review-eval",
        help="A/B review benchmark: claude reviews closed PRs with and without the graph",
    )
    rsub = cmd.add_subparsers(dest="review_eval_command")

    add = rsub.add_parser("add-case", help="Add a merged GitHub PR as a review case")
    add.add_argument("--gh-repo", required=True, help="owner/name on GitHub")
    add.add_argument("--pr", required=True, type=int, nargs="+", help="PR number(s)")
    add.add_argument("--source", required=True, help="Local clone holding the PR commits")
    add.add_argument("--cases", default=str(DEFAULT_CASES), help="Cases YAML file")

    run = rsub.add_parser("run", help="Run the review arms over the cases")
    run.add_argument("--cases", default=str(DEFAULT_CASES), help="Cases YAML file")
    run.add_argument("--case", nargs="+", default=None, help="Only these case ids")
    run.add_argument(
        "--arms", default="baseline,graph,graph_required",
        help="Comma-separated arms (baseline, graph, graph_required, graph_md, "
             "graph_md_enrich)",
    )
    run.add_argument("--reps", type=int, default=1, help="Repetitions per arm")
    run.add_argument("--model", default=DEFAULT_MODEL, help="Reviewer model")
    run.add_argument("--effort", default=DEFAULT_EFFORT, help="Reviewer effort level")
    run.add_argument(
        "--max-budget-usd", type=float, default=DEFAULT_BUDGET_USD,
        help="Spend cap per review run",
    )
    run.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="Seconds per review run",
    )
    run.add_argument("--out", default=str(DEFAULT_OUT), help="Directory for run output")
    run.add_argument(
        "--workdir", default=None, help="Directory for sanitized clones (default: temp/rab)",
    )
    run.add_argument("--fresh", action="store_true", help="Re-create clones and graphs")
    run.add_argument(
        "--isolation", choices=["restricted", "project"], default="restricted",
        help="restricted: no settings, no CLAUDE.md. project: loads the clone's "
             "CLAUDE.md (required by graph_md arms); the user CLAUDE.md loads too, "
             "user hooks do not",
    )
    run.add_argument("--claude-bin", default="claude", help="claude executable")

    judge = rsub.add_parser("judge", help="Blind-judge the reviews of a run")
    judge.add_argument("--run", required=True, help="Run directory (holds records.jsonl)")
    judge.add_argument("--cases", default=str(DEFAULT_CASES), help="Cases YAML file")
    judge.add_argument("--case", nargs="+", default=None, help="Only these case ids")
    judge.add_argument("--model", default=None, help="Judge model (default: Opus)")
    judge.add_argument("--effort", default=DEFAULT_EFFORT, help="Judge effort level")
    judge.add_argument("--workdir", default=None, help="Directory for sanitized clones")
    judge.add_argument("--claude-bin", default="claude", help="claude executable")

    mine = rsub.add_parser("mine", help="SZZ: later fix commits blamed back to each case")
    mine.add_argument("--cases", default=str(DEFAULT_CASES), help="Cases YAML file")
    mine.add_argument("--case", nargs="+", default=None, help="Only these case ids")
    mine.add_argument(
        "--write", action="store_true",
        help="Store candidates as unconfirmed known_issues (set confirmed: true to use them)",
    )

    audit = rsub.add_parser("audit", help="Recompute leak flags of a run from its streams")
    audit.add_argument("--run", required=True, help="Run directory")
    audit.add_argument("--cases", default=str(DEFAULT_CASES), help="Cases YAML file")
    audit.add_argument("--workdir", default=None, help="Directory for sanitized clones")

    report = rsub.add_parser("report", help="Score a judged run and write report.md")
    report.add_argument("--run", required=True, help="Run directory")
    report.add_argument("--cases", default=str(DEFAULT_CASES), help="Cases YAML file")
    return cmd


def _select(cases, wanted):
    if not wanted:
        return cases
    missing = set(wanted) - {c.id for c in cases}
    if missing:
        raise SystemExit(f"unknown case ids: {sorted(missing)}")
    return [c for c in cases if c.id in set(wanted)]


def handle(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Dispatch a parsed ``review-eval`` command."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if args.review_eval_command == "add-case":
        from .cases import case_from_github, upsert_case

        for pr in args.pr:
            case = case_from_github(args.gh_repo, pr, Path(args.source))
            upsert_case(case, Path(args.cases))
            print(f"added {case.id}: {case.title}")
        return

    if args.review_eval_command == "run":
        from .cases import load_cases
        from .runner import RunAbortedError, RunSettings, run_cases
        from .sandbox import default_workdir

        cases = _select(load_cases(Path(args.cases)), args.case)
        settings = RunSettings(
            model=args.model,
            effort=args.effort,
            max_budget_usd=args.max_budget_usd,
            timeout_s=args.timeout,
            claude_bin=args.claude_bin,
            isolation=args.isolation,
        )
        try:
            run_dir = run_cases(
                cases,
                arms=tuple(a.strip() for a in args.arms.split(",") if a.strip()),
                reps=args.reps,
                settings=settings,
                out_dir=Path(args.out),
                workdir=Path(args.workdir) if args.workdir else default_workdir(),
                fresh=args.fresh,
            )
        except RunAbortedError as exc:
            raise SystemExit(f"stopped: {exc}") from exc
        print(f"records: {run_dir / 'records.jsonl'}")
        return

    if args.review_eval_command == "judge":
        from .cases import load_cases
        from .judge import JUDGE_MODEL, judge_case, judge_settings
        from .runner import RunSettings
        from .sandbox import default_workdir

        run_dir = Path(args.run)
        run_meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        cases = [c for c in load_cases(Path(args.cases)) if c.id in run_meta["cases"]]
        settings = judge_settings(
            RunSettings(effort=args.effort, claude_bin=args.claude_bin),
            model=args.model or JUDGE_MODEL,
        )
        for case in _select(cases, args.case):
            path = judge_case(
                case, run_dir, settings,
                workdir=Path(args.workdir) if args.workdir else default_workdir(),
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            issues = (payload.get("judgment") or {}).get("issues") or []
            print(
                f"{case.id}: {payload['status']} ${payload['total_cost_usd']} "
                f"issues={len(issues)} assignment={payload['assignment']}"
            )
        return

    if args.review_eval_command == "mine":
        from dataclasses import replace

        from .cases import load_cases, save_cases
        from .mine import mine_case

        path = Path(args.cases)
        cases = load_cases(path)
        selected = {c.id for c in _select(cases, args.case)}
        updated = []
        for case in cases:
            if case.id not in selected:
                updated.append(case)
                continue
            found = mine_case(case)
            print(f"{case.id}: {len(found)} candidate(s)")
            for cand in found:
                blamed = ", ".join(f"{h['file']}:{h['lines']}" for h in cand["blamed_lines"])
                print(f"  {cand['id']} {cand['fix_subject'][:90]}  <- {blamed[:160]}")
            if args.write:
                ids = {k.get("id") for k in case.known_issues}
                new = [c for c in found if c["id"] not in ids]
                case = replace(case, known_issues=[*case.known_issues, *new])
            updated.append(case)
        if args.write:
            save_cases(updated, path)
            print(f"written to {path} (unconfirmed; review and set confirmed: true)")
        return

    if args.review_eval_command == "audit":
        from .cases import load_cases
        from .runner import reaudit_run
        from .sandbox import default_workdir

        changed = reaudit_run(
            Path(args.run), load_cases(Path(args.cases)),
            Path(args.workdir) if args.workdir else default_workdir(),
        )
        print(f"{changed} record(s) updated")
        return

    if args.review_eval_command == "report":
        from .cases import load_cases
        from .score import load_run, render_markdown

        run_dir = Path(args.run)
        known = {
            c.id: [k.get("id") for k in c.confirmed_issues()]
            for c in load_cases(Path(args.cases))
        } if Path(args.cases).exists() else {}
        scored = load_run(run_dir, known)
        report_path = run_dir / "report.md"
        report_path.write_text(render_markdown(scored), encoding="utf-8")
        (run_dir / "scores.json").write_text(
            json.dumps({"by_arm": scored["by_arm"], "rows": scored["rows"]},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"report: {report_path}")
        return

    parser.print_help()

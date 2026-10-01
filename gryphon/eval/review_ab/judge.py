"""Blind judging: one call per case sees every review under a random label.

The judge merges findings that describe the same problem into issues,
verifies each issue in the sanitized baseline clone (no graph tools, so the
verdicts do not lean on either arm's evidence source), and scores each review
on a fixed rubric. The label → arm/rep map is written next to the judgment and
never shown to the judge.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from .cases import ReviewCase
from .prompts import SEVERITIES
from .runner import RunSettings, _child_env, _run_claude, build_command, parse_stream, summarize
from .sandbox import BASE_BRANCH, REVIEW_BRANCH, changed_files, prepare_arm

logger = logging.getLogger(__name__)

JUDGE_MODEL = "claude-opus-5-5"
JUDGE_VERSION = "2"
# Per-candidate cap on the later fix diff shown to the judge, and in total.
FIX_DIFF_CHARS = 3000
FIX_DIFF_TOTAL_CHARS = 24000
VERDICTS = ("real", "false", "unverifiable")
RUBRIC = ("correctness", "impact", "tests", "signal", "actionability")

JUDGE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["issues", "reviews", "known_verdicts"],
    "properties": {
        "known_verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "is_defect", "rationale"],
                "properties": {
                    "id": {"type": "string"},
                    "is_defect": {"type": "boolean"},
                    "rationale": {"type": "string"},
                },
            },
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id", "title", "file", "verdict", "severity", "rationale",
                    "known_issue", "reported_by",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "file": {"type": "string"},
                    "verdict": {"enum": list(VERDICTS)},
                    "severity": {"enum": [*SEVERITIES, "none"]},
                    "rationale": {"type": "string"},
                    "known_issue": {"type": ["string", "null"]},
                    "reported_by": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["review", "finding"],
                            "properties": {
                                "review": {"type": "string"},
                                "finding": {"type": "integer"},
                            },
                        },
                    },
                },
            },
        },
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["review", "scores", "comment"],
                "properties": {
                    "review": {"type": "string"},
                    "scores": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": list(RUBRIC),
                        "properties": {
                            k: {"type": "integer", "minimum": 1, "maximum": 5} for k in RUBRIC
                        },
                    },
                    "comment": {"type": "string"},
                },
            },
        },
    },
}

_PROMPT = """\
You are the judge in a blind comparison of code reviews. Several reviewers
independently reviewed the same pull request in the Git repository in the
current directory. Decide which of their findings are real, merge duplicates,
and score each review.

The PR branch `{review}` is checked out. It merges into `{base}`; the change
under review is `git diff {base}...{review}`.

Title: {title}

Description:
{body}

Changed files (added/deleted lines):
{files}

Review labels are random. They say nothing about how a review was produced,
and their order means nothing.

{reviews}

Known issues. Each is a later commit whose removed or changed lines were
written by this PR. CONFIRMED ones are defects a person verified.
CANDIDATE ones were found automatically and may also be refactors,
formatting, renames or follow-up features; their later diff is shown.
{known}

Instructions:
1. Group findings that describe the same underlying defect into one issue,
   even when worded differently or anchored on different lines. Every finding
   (review label + finding number) belongs to exactly one issue.
2. Verify every issue in the code at `{review}`, and on `{base}` for the
   pre-change behavior (`git show {base}:<path>`). Verdict:
   - real: the defect exists and this change introduced or exposed it;
   - false: the code does not behave as claimed, or the behavior is
     intended or harmless;
   - unverifiable: it depends on runtime data or context you cannot check.
   Set the severity you judge (blocker, major, minor; none for false), not
   the reviewer's. The rationale cites the code that decided the verdict.
3. If an issue matches a known issue (confirmed or candidate), set
   `known_issue` to its id; else null.
4. Score each review from 1 to 5; the scores must follow from your verdicts:
   - correctness: how many of its claims are true;
   - impact: whether it found consequences outside the diff (callers, other
     flows, data already stored);
   - tests: whether it identified missing or wrong tests correctly;
   - signal: share of useful content versus noise and false alarms;
   - actionability: whether the author can act on it as written.
5. For every CANDIDATE known issue, add an entry to `known_verdicts`:
   `is_defect` is true only when the later diff corrects wrong behavior
   that this PR introduced; false for refactors, renames, formatting, new
   features or changed requirements. The rationale cites the diff. Leave
   `known_verdicts` empty when there are no candidates.
6. Do not modify, create or delete files. There is no network access.

Write `title`, `rationale` and `comment` in the language of the PR title.
"""


def _labels(case_id: str, run_id: str, n: int) -> list[str]:
    """A deterministic shuffle of R1..Rn for this case and run.

    Ordering by a hash of (case, run, label) shuffles without a PRNG: the
    order only needs to be unpredictable to the judge, not secret.
    """
    labels = [f"R{i + 1}" for i in range(n)]
    return sorted(
        labels,
        key=lambda label: hashlib.sha256(f"{case_id}:{run_id}:{label}".encode()).hexdigest(),
    )


def judgeable(records: list[dict], case_id: str) -> list[dict]:
    """Reviews of *case_id* that finished and did not leak."""
    return [
        r for r in records
        if r["case_id"] == case_id and r["status"] == "ok" and not r["leak_flags"]
    ]


# Tool and product names that would tell the judge which arm wrote a review.
_ARM_TELL_RE = re.compile(
    r"mcp__\w+|\b\w+_tool\b|gryphon|code-review-graph|knowledge graph|grafo de c[óo]digo",
    re.I,
)


def scrub(text: str) -> str:
    """Remove arm-revealing tool names from reviewer text."""
    return _ARM_TELL_RE.sub("[tool]", text or "")


def _format_review(label: str, record: dict) -> str:
    summary = scrub(record.get("summary") or "(none)")
    lines = [f"### Review {label}", "", f"Summary: {summary}", ""]
    findings = record.get("findings") or []
    if not findings:
        lines.append("Findings: none.")
    for i, f in enumerate(findings):
        where = f"{f['file']}:{f['line']}" if f.get("line") is not None else f["file"]
        lines.append(f"F{i}. [{f['severity']}/{f['category']}] {where} — {scrub(f['claim'])}")
        lines.append(f"    Evidence: {scrub(f['evidence'])}")
    return "\n".join(lines)


def build_judge_prompt(
    case: ReviewCase, files: list[dict], labeled: list[tuple[str, dict]],
) -> str:
    from .prompts import _format_files

    known = _format_known(case)
    return _PROMPT.format(
        review=REVIEW_BRANCH,
        base=BASE_BRANCH,
        title=case.title.strip() or "(no title)",
        body=case.body.strip() or "(no description)",
        files=_format_files(files),
        reviews="\n\n".join(_format_review(label, rec) for label, rec in labeled),
        known=known,
    )


def _fix_diff(case: ReviewCase, issue: dict) -> str:
    """The later fix's diff on the blamed files, as the judge's reference.

    Read from the source repository: the judge may see the future, only the
    reviewers may not.
    """
    sha = issue.get("fix_commit")
    if not sha:
        return ""
    files = sorted({h["file"] for h in issue.get("blamed_lines", []) if h.get("file")})
    try:
        out = subprocess.run(
            ["git", "show", "--no-color", "--format=%s", "--unified=3", sha, "--", *files],
            cwd=case.source_repo, capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("fix diff unavailable for %s: %s", sha[:8], exc)
        return "(diff unavailable)"
    if len(out) > FIX_DIFF_CHARS:
        out = out[:FIX_DIFF_CHARS] + "\n... (truncated)"
    return out


def _format_known(case: ReviewCase) -> str:
    """Known issues for the prompt: confirmed ones, then candidates with diffs."""
    if not case.known_issues:
        return "(none recorded)"
    lines: list[str] = []
    budget = FIX_DIFF_TOTAL_CHARS
    for k in case.known_issues:
        confirmed = k.get("confirmed", True)
        tag = "CONFIRMED" if confirmed else "CANDIDATE"
        lines.append(f"- {k.get('id')} [{tag}] {k.get('file', '')} — {k.get('description', '')}")
        if not confirmed:
            diff = _fix_diff(case, k) if budget > 0 else "(diff omitted: prompt budget spent)"
            budget -= len(diff)
            lines.append("  Later diff:\n" + "\n".join("    " + d for d in diff.splitlines()))
    return "\n".join(lines)


def check_assignment(judgment: dict, labeled: list[tuple[str, dict]]) -> dict:
    """Findings the judge left out or assigned twice, keyed ``R<n>:F<i>``."""
    expected = {
        f"{label}:F{i}"
        for label, rec in labeled
        for i in range(len(rec.get("findings") or []))
    }
    seen: dict[str, int] = {}
    for issue in judgment.get("issues", []):
        for ref in issue.get("reported_by", []):
            key = f"{ref['review']}:F{ref['finding']}"
            seen[key] = seen.get(key, 0) + 1
    return {
        "unassigned": sorted(expected - set(seen)),
        "duplicated": sorted(k for k, n in seen.items() if n > 1),
        "unknown": sorted(set(seen) - expected),
    }


def judge_case(
    case: ReviewCase,
    run_dir: Path,
    settings: RunSettings,
    *,
    workdir: Path,
) -> Path:
    """Judge every usable review of *case* in *run_dir*; returns the judgment path."""
    records = [
        json.loads(line)
        for line in (run_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    reviews = judgeable(records, case.id)
    if not reviews:
        raise ValueError(f"{run_dir}: no judgeable reviews for {case.id}")
    labels = _labels(case.id, run_dir.name, len(reviews))
    labeled = sorted(zip(labels, reviews), key=lambda pair: int(pair[0][1:]))

    sandbox = prepare_arm(case, "baseline", workdir)
    files = changed_files(sandbox)
    prompt = build_judge_prompt(case, files, labeled)

    out_dir = run_dir / "judgments"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{case.id}.prompt.txt").write_text(prompt, encoding="utf-8")
    (out_dir / f"{case.id}.labels.json").write_text(json.dumps({
        label: {"arm": rec["arm"], "rep": rec["rep"], "stream": rec["stream"]}
        for label, rec in labeled
    }, indent=2), encoding="utf-8")
    stream_path = out_dir / f"{case.id}.stream.jsonl"

    started = time.monotonic()
    timed_out = False
    exit_code = None
    with open(stream_path, "w", encoding="utf-8") as out, open(
        out_dir / f"{case.id}.stderr", "w", encoding="utf-8"
    ) as err:
        try:
            proc = _run_claude(
                build_command(sandbox, settings, schema=JUDGE_SCHEMA),
                cwd=str(sandbox.repo), input=prompt, stdout=out, stderr=err,
                text=True, encoding="utf-8", errors="replace",
                env=_child_env(sandbox), timeout=settings.timeout_s,
            )
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            logger.warning("judge for %s timed out", case.id)

    with open(stream_path, encoding="utf-8", errors="replace") as f:
        parsed = parse_stream(f)
    result = parsed["result"] or {}
    judgment = result.get("structured_output") if isinstance(result, dict) else None
    status = summarize(parsed, exit_code=exit_code, timed_out=timed_out)["status"]
    if status == "no_structured_output":
        status = "no_judgment"
    payload = {
        "case_id": case.id,
        "run_id": run_dir.name,
        "judge_model": settings.model,
        "judge_effort": settings.effort,
        "judge_version": JUDGE_VERSION,
        "status": status,
        "wall_seconds": round(time.monotonic() - started, 1),
        "total_cost_usd": result.get("total_cost_usd"),
        "num_turns": result.get("num_turns"),
        "assignment": check_assignment(judgment, labeled) if judgment else None,
        "judgment": judgment,
    }
    path = out_dir / f"{case.id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def judge_settings(base: RunSettings, model: str = JUDGE_MODEL) -> RunSettings:
    """Judge defaults: a stronger model and more budget and time than a review."""
    return replace(
        base, model=model,
        max_budget_usd=max(base.max_budget_usd, 8.0),
        timeout_s=max(base.timeout_s, 3600),
    )

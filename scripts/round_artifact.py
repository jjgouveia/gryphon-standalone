#!/usr/bin/env python3
"""Compose a re-review round into the body a privileged job will post.

The agent produces two files: the review prose, and ``verdicts.json``
carrying the findings for this round. This script merges them into one
body with a freshly rendered state ledger, and writes the metadata the
privileged workflow validates before posting.

The split matters. Agent output is untrusted and prose-shaped; the ledger
is the resume contract. Building it here rather than asking the agent to
write it means the escaping, the schema check and the round bookkeeping
cannot be skipped by a model that forgot a rule in a long context.

``verdicts.json`` is an object with ``findings`` and the round metadata::

    {"flag": "request-changes",
     "findings": [{"id": 1, "status": "open", "sev": "high",
                   "title": "...", "files": ["gryphon/parser.py"]}]}

Exit codes:
    0  body composed
    2  an input file could not be read
    3  verdicts.json did not satisfy the schema
    4  the composed body does not carry the ledger it should
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).parent))

from pr_ledger import MARKER, Finding, Ledger, LedgerError, extract, parse_block
from pr_watch import GitHubError, escalate, newest_ledger, render_round_body

logger = logging.getLogger("round_artifact")

VALID_FLAGS = ("comment", "request-changes")
PROSE_LIMIT = 40000


class VerdictError(ValueError):
    """verdicts.json does not satisfy the round contract."""


def resolve_flag(
    proposed: str,
    findings: Iterable[Finding],
    previous: Ledger | None,
) -> str:
    """The flag this round may actually publish.

    The agent proposes one; the unattended policy has the last word. A
    round that proposes ``request-changes`` on a finding no earlier round
    raised is the bot's first disagreement with new code, so it is
    downgraded to a comment. Anything else it proposed is kept.
    """
    return escalate(previous, findings)


def parse_verdicts(raw: str) -> tuple[str, tuple[Finding, ...], int]:
    """Read ``verdicts.json``.

    Returns the flag, the findings and the round number. The flag is
    checked against the unattended policy: an unattended round may comment
    or request changes, never approve.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VerdictError(f"verdicts.json is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise VerdictError("verdicts.json must be an object")

    flag = payload.get("flag")
    if flag not in VALID_FLAGS:
        raise VerdictError(
            f"flag {flag!r} is not one of {VALID_FLAGS}; an unattended round "
            "may never approve"
        )

    round_number = payload.get("round")
    if not isinstance(round_number, int) or isinstance(round_number, bool) or round_number < 1:
        raise VerdictError("round must be an integer >= 1")

    raw_findings = payload.get("findings")
    findings = parse_findings(raw_findings)
    return flag, findings, round_number


def parse_findings(raw_findings: object) -> tuple[Finding, ...]:
    """Validate a findings list against the ledger schema.

    Shared by the unattended path (which reads ``verdicts.json``) and the
    interactive one (which publishes a review directly), so a finding that
    cannot be rendered is rejected in both before it is published rather
    than after the next round tries to read it.
    """
    if not isinstance(raw_findings, list):
        raise VerdictError("findings must be a list")

    findings: list[Finding] = []
    seen: set[int] = set()
    for item in raw_findings:
        if not isinstance(item, dict):
            raise VerdictError("each finding must be an object")
        ident = item.get("id")
        if not isinstance(ident, int) or isinstance(ident, bool) or ident < 1:
            raise VerdictError("finding id must be an integer >= 1")
        if ident in seen:
            raise VerdictError(f"duplicate finding id {ident}")
        seen.add(ident)

        document = json.dumps(
            {
                "v": 1,
                "head": "0" * 40,
                "base": "placeholder",
                "round": 1,
                "flag": "comment",
                "findings": [item],
            }
        )
        try:
            parsed = parse_block(document)
        except LedgerError as exc:
            raise VerdictError(f"finding {ident}: {exc}") from exc
        findings.append(parsed.findings[0])

    return tuple(findings)


def compose(
    prose: str,
    findings: tuple[Finding, ...],
    *,
    head: str,
    base: str,
    round_number: int,
    flag: str,
) -> str:
    ledger = Ledger(
        head=head, base=base, round=round_number, flag=flag, findings=findings
    )
    return render_round_body(ledger, findings, prose)


def _previous_ledger(args: argparse.Namespace) -> Ledger | None:
    """The ledger from the round before this one, if there was one.

    Read from an explicit file when given, otherwise from the thread. A
    failure here is not fatal: with no previous ledger the escalation rule
    has no carried ids, so the round degrades to a comment, which is the
    safe direction. Losing the thread would otherwise downgrade every
    escalation silently, so a read failure is logged rather than swallowed.
    """
    if args.previous_ledger is not None:
        try:
            return extract(args.previous_ledger.read_text(encoding="utf-8"))
        except (OSError, LedgerError) as exc:
            logger.warning("could not read %s: %s", args.previous_ledger, exc)
            return None

    if not args.repo:
        logger.warning(
            "no --repo or --previous-ledger given: the escalation rule has no "
            "carried ids, so this round can only comment"
        )
        return None

    try:
        return newest_ledger(args.repo, args.pr)
    except GitHubError as exc:
        logger.warning("could not read the thread for %s#%s: %s", args.repo, args.pr, exc)
        return None


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compose a re-review round body from agent output."
    )
    parser.add_argument("--prose", required=True, type=Path)
    parser.add_argument("--verdicts", required=True, type=Path)
    parser.add_argument("--pr", required=True, type=int, help="PR number")
    parser.add_argument(
        "--repo",
        help="owner/name, to read the previous ledger for the escalation rule",
    )
    parser.add_argument(
        "--previous-ledger",
        type=Path,
        help="A ledger body from the prior round, instead of reading the thread",
    )
    parser.add_argument("--head", required=True, help="Head SHA this round verified")
    parser.add_argument("--base", required=True, help="Base ref at this round")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    try:
        prose = args.prose.read_text(encoding="utf-8")
        raw = args.verdicts.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("could not read the agent output: %s", exc)
        return 2

    try:
        proposed, findings, round_number = parse_verdicts(raw)
    except VerdictError as exc:
        logger.error("%s", exc)
        return 3

    prose = prose.strip()
    if not prose:
        logger.error("the agent produced no prose")
        return 3
    if MARKER in prose:
        # The agent read the previous review body to resume, and quoted it.
        # A second ledger in the body is not merely untidy: the reader takes
        # the last block, so a stale copy in the prose is inert, but it means
        # the agent treated the ledger as content rather than as state. Fail
        # here, where the cause is visible, instead of publishing a body with
        # two blocks in it.
        logger.error(
            "the prose contains a %s block; the ledger is composed here, not "
            "by the agent",
            MARKER,
        )
        return 3
    if len(prose.encode("utf-8")) > PROSE_LIMIT:
        logger.error("the agent prose exceeds %d bytes", PROSE_LIMIT)
        return 3

    previous = _previous_ledger(args)
    flag = resolve_flag(proposed, findings, previous)
    if flag != proposed:
        logger.warning(
            "the round proposed %s; publishing %s because no high finding from a "
            "previous round is still open",
            proposed,
            flag,
        )

    body = compose(
        prose,
        findings,
        head=args.head,
        base=args.base,
        round_number=round_number,
        flag=flag,
    )

    # The composed body must survive its own parser. A body that cannot be
    # read back would end the loop at the next round.
    try:
        recovered = extract(body)
    except LedgerError as exc:
        logger.error("the composed body does not carry a readable ledger: %s", exc)
        return 4
    if recovered is None or recovered.head != args.head:
        logger.error(
            "the composed body does not carry a readable ledger for %s", args.head
        )
        return 4

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "body.md").write_bytes(body.encode("utf-8"))
    (args.out / "meta.json").write_text(
        json.dumps(
            {
                "pr_number": args.pr,
                "head": args.head,
                "base": args.base,
                "round": round_number,
                "flag": flag,
                "open_findings": len(recovered.open_findings),
            }
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main())

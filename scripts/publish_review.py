#!/usr/bin/env python3
"""Publish a review, with the ledger it needs to be resumable.

This is the interactive publish path, and it exists because the ledger was
optional in practice. The skill says every published review carries one,
but a review written by hand and posted with ``gh pr review`` has none — and
nothing complains. The watcher then has nothing to resume from, and the loop
dies quietly on exactly the reviews a human cared enough to write.

So the publish path composes the body itself: it takes the prose and the
findings, renders the ledger, and refuses to publish a body it cannot read
back. There is no flag to skip the ledger, because the failure it prevents
is invisible at the moment it happens.

    python scripts/publish_review.py \
        --repo Ativos-Tecnologia/cvld --pr 2145 \
        --flag request-changes \
        --prose prose.md --verdicts verdicts.json \
        --head <sha> --base main

The round number is derived from the ledger already in the thread, so a
re-review continues the numbering instead of restarting it. ``verdicts.json``
carries only ``findings``; the flag is a command-line argument because a
human picks it, and the policy that constrains it belongs to the unattended
path rather than here.

Exit codes:
    0  published
    2  an input file could not be read
    3  the prose or the findings did not satisfy the contract
    4  the composed body does not carry the ledger it should
    5  gh refused to publish
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pr_ledger import MARKER, Ledger, LedgerError, extract
from pr_watch import GitHubError, newest_ledger, render_round_body
from round_artifact import VerdictError, parse_findings

logger = logging.getLogger("publish_review")

FLAGS = ("comment", "request-changes", "approve")
PROSE_LIMIT = 40000


def next_round(repo: str, pr: int) -> int:
    """The round this review is, continuing the thread's numbering.

    Round 1 when the thread has no ledger. Otherwise the previous round
    plus one: renumbering from 1 would re-open every finding a previous
    round already settled.
    """
    try:
        previous = newest_ledger(repo, pr)
    except GitHubError as exc:
        logger.warning("could not read the thread for %s#%s: %s", repo, pr, exc)
        return 1
    return previous.round + 1 if previous is not None else 1


def compose(prose: str, findings, *, head: str, base: str, round_number: int, flag: str) -> str:
    ledger = Ledger(
        head=head, base=base, round=round_number, flag=flag, findings=tuple(findings)
    )
    return render_round_body(ledger, findings, prose)


def verify(body: str, *, head: str, flag: str) -> None:
    """Refuse a body that cannot be read back as the ledger it claims.

    The round-trip is the whole guard: a body that does not parse would end
    the loop at the next round, and it would do so silently, long after the
    person who published it stopped looking.
    """
    try:
        recovered = extract(body)
    except LedgerError as exc:
        raise LedgerError(f"the composed body does not carry a readable ledger: {exc}") from exc
    if recovered is None:
        raise LedgerError("the composed body does not carry a state ledger")
    if recovered.head != head:
        raise LedgerError(
            f"the ledger records head {recovered.head[:8]} but this review is for {head[:8]}"
        )
    if recovered.flag != flag:
        raise LedgerError(
            f"the ledger records flag {recovered.flag!r} but this review publishes {flag!r}"
        )


def publish(repo: str, pr: int, flag: str, body_file: Path, *, closed: bool = False) -> None:
    """Post the review, or a comment when the PR cannot take one."""
    command = ["gh", "pr", "review", str(pr), "--repo", repo, "--body-file", str(body_file)]
    if closed:
        command = ["gh", "pr", "comment", str(pr), "--repo", repo, "--body-file", str(body_file)]
    else:
        command.append(f"--{flag}")

    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0:
        raise GitHubError(done.stderr.strip() or f"gh exited {done.returncode}")


def pr_is_open(repo: str, pr: int) -> bool:
    done = subprocess.run(
        ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "state"],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise GitHubError(done.stderr.strip() or f"gh exited {done.returncode}")
    try:
        return json.loads(done.stdout).get("state") == "OPEN"
    except json.JSONDecodeError as exc:
        raise GitHubError(f"could not read the PR state: {exc}") from exc


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish a review with the state ledger it needs."
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--flag", required=True, choices=FLAGS)
    parser.add_argument("--prose", required=True, type=Path, help="The review text")
    parser.add_argument(
        "--verdicts",
        type=Path,
        help="JSON with a findings list; omit for a review with no findings",
    )
    parser.add_argument("--head", required=True, help="Head SHA this review verified")
    parser.add_argument("--base", required=True, help="Base ref")
    parser.add_argument("--round", type=int, help="Override the derived round number")
    parser.add_argument(
        "--dry-run", action="store_true", help="Compose and verify, but do not publish"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    try:
        # utf-8-sig reads plain UTF-8 and strips a leading BOM if some
        # editor added one. The prose is written by hand often enough that
        # a BOM is a realistic accident, and it would otherwise show up as
        # a JSON parse error or a stray character in the published review.
        prose = args.prose.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        logger.error("could not read the prose: %s", exc)
        return 2

    if not prose:
        logger.error("the prose is empty")
        return 3
    if len(prose.encode("utf-8")) > PROSE_LIMIT:
        logger.error("the prose exceeds %d bytes", PROSE_LIMIT)
        return 3
    if MARKER in prose:
        logger.error(
            "the prose contains a %s block; the ledger is composed here, not "
            "written by hand",
            MARKER,
        )
        return 3

    findings: tuple = ()
    if args.verdicts is not None:
        try:
            payload = json.loads(args.verdicts.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("could not read the findings: %s", exc)
            return 2
        try:
            findings = parse_findings(
                payload.get("findings") if isinstance(payload, dict) else payload
            )
        except VerdictError as exc:
            logger.error("%s", exc)
            return 3

    round_number = args.round if args.round is not None else next_round(args.repo, args.pr)

    body = compose(
        prose,
        findings,
        head=args.head,
        base=args.base,
        round_number=round_number,
        flag=args.flag,
    )

    try:
        verify(body, head=args.head, flag=args.flag)
    except LedgerError as exc:
        logger.error("%s", exc)
        return 4

    if args.dry_run:
        print(body)
        return 0

    try:
        closed = not pr_is_open(args.repo, args.pr)
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(body)
            body_file = Path(handle.name)
        publish(args.repo, args.pr, args.flag, body_file, closed=closed)
        body_file.unlink(missing_ok=True)
    except (GitHubError, OSError) as exc:
        logger.error("%s", exc)
        return 5

    print(f"published round {round_number} on {args.repo}#{args.pr} as {args.flag}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

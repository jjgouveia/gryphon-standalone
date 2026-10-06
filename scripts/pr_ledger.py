#!/usr/bin/env python3
"""Read, write and interpret the ``review-pr`` state ledger.

The ledger is a one-line JSON footer that every published ``review-pr``
review carries::

    <!-- gryphon-review-state {"v":1,"head":"<sha>","round":2,...} -->

It exists so a re-review round can be resumed from the PR thread alone:
the round number, the head SHA the pass verified and the open findings
all travel in the review body, so a fresh agent session continues without
any local state.

This module is the deterministic half of that contract. Parsing, escaping,
round numbering and the resume decision are mechanical, so they live here
where they can be tested, rather than in prose inside SKILL.md where every
round re-derives them.

Because the block sits inside an HTML comment, ``<``, ``>`` and ``&`` in
the JSON strings are written as JSON unicode escapes. A literal ``-->``
in a finding title would otherwise close the comment early and leave a
truncated ledger for the next round to misread.

Exit codes (CLI):
    0  parsed, or no ledger present when ``--optional``
    2  input could not be read
    3  a ledger was present but is not valid at this schema version
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

logger = logging.getLogger("pr_ledger")

MARKER = "<!-- gryphon-review-state"
SCHEMA_VERSION = 1

VALID_FLAGS = ("comment", "request-changes", "approve")
VALID_STATUS = ("open", "resolved", "obsolete")
VALID_SEVERITIES = ("high", "medium", "low")

# A ledger block runs to the end of its comment. Bound it so a body that
# contains a second comment after the ledger cannot fold unrelated text
# into the JSON.
_BLOCK_RE = re.compile(
    re.escape(MARKER) + r"\s*(\{.*?\})\s*-->", re.DOTALL
)
_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")

Decision = Literal[
    "first-pass",
    "newer-schema",
    "retargeted",
    "already-reviewed",
    "rounds-exhausted",
    "resume",
]


class LedgerError(ValueError):
    """The block is present but does not satisfy the schema."""


@dataclass(frozen=True)
class Finding:
    id: int
    status: str
    sev: str
    title: str
    files: tuple[str, ...] = ()

    @property
    def is_open(self) -> bool:
        return self.status == "open"


@dataclass(frozen=True)
class Ledger:
    head: str
    base: str
    round: int
    flag: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def open_findings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.is_open)

    @property
    def open_files(self) -> frozenset[str]:
        """Every file an open finding touches.

        The cheap resume filter compares a delta against this set, so a
        push that cannot affect an open finding never costs a round.
        """
        paths: set[str] = set()
        for finding in self.open_findings:
            paths.update(finding.files)
        return frozenset(paths)

    def next_id(self) -> int:
        """First unused id. Ids are assigned once and never reused."""
        return max((f.id for f in self.findings), default=0) + 1

    def with_verdicts(
        self, findings: Iterable[Finding], *, flag: str, round_number: int
    ) -> "Ledger":
        """A ledger for the next round: same ids, new statuses."""
        return Ledger(
            head=self.head,
            base=self.base,
            round=round_number,
            flag=flag,
            findings=tuple(findings),
        )


def _escape_for_comment(text: str) -> str:
    """JSON-unicode-escape the three characters that can close the block.

    Applied to the serialized document, not to the pre-serialized values:
    JSON's own structural characters never include these, so a global
    replace cannot touch anything outside a string value.
    """
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise LedgerError(f"field {key!r} must be a non-empty string")
    return value


def _parse_finding(raw: Any) -> Finding:
    if not isinstance(raw, dict):
        raise LedgerError("each finding must be an object")

    ident = raw.get("id")
    if not isinstance(ident, int) or isinstance(ident, bool) or ident < 1:
        raise LedgerError("finding id must be an integer >= 1")

    status = _require_str(raw, "status")
    if status not in VALID_STATUS:
        raise LedgerError(f"finding status {status!r} is not one of {VALID_STATUS}")

    sev = _require_str(raw, "sev")
    if sev not in VALID_SEVERITIES:
        raise LedgerError(f"finding severity {sev!r} is not one of {VALID_SEVERITIES}")

    files = raw.get("files", [])
    if not isinstance(files, list) or not all(isinstance(p, str) for p in files):
        raise LedgerError("finding files must be a list of strings")

    return Finding(
        id=ident,
        status=status,
        sev=sev,
        title=_require_str(raw, "title"),
        files=tuple(files),
    )


def parse_block(text: str) -> Ledger:
    """Parse one ledger document. Raises :class:`LedgerError` if invalid."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LedgerError(f"ledger is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise LedgerError("ledger must be a JSON object")

    version = payload.get("v")
    if version != SCHEMA_VERSION:
        raise LedgerError(f"schema version {version!r} is not {SCHEMA_VERSION}")

    head = _require_str(payload, "head")
    if not _SHA_RE.match(head):
        raise LedgerError("head must be a full 40-character lowercase SHA")

    round_number = payload.get("round")
    if not isinstance(round_number, int) or isinstance(round_number, bool) or round_number < 1:
        raise LedgerError("round must be an integer >= 1")

    flag = _require_str(payload, "flag")
    if flag not in VALID_FLAGS:
        raise LedgerError(f"flag {flag!r} is not one of {VALID_FLAGS}")

    raw_findings = payload.get("findings", [])
    if not isinstance(raw_findings, list):
        raise LedgerError("findings must be a list")

    findings = tuple(_parse_finding(item) for item in raw_findings)

    seen: set[int] = set()
    for finding in findings:
        if finding.id in seen:
            raise LedgerError(f"duplicate finding id {finding.id}")
        seen.add(finding.id)

    return Ledger(
        head=head,
        base=_require_str(payload, "base"),
        round=round_number,
        flag=flag,
        findings=findings,
    )


def render(ledger: Ledger) -> str:
    """Serialize a ledger to the one-line block, escaped for the comment."""
    document = json.dumps(
        {
            "v": SCHEMA_VERSION,
            "head": ledger.head,
            "base": ledger.base,
            "round": ledger.round,
            "flag": ledger.flag,
            "findings": [
                {
                    "id": f.id,
                    "status": f.status,
                    "sev": f.sev,
                    "title": f.title,
                    "files": list(f.files),
                }
                for f in ledger.findings
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{MARKER} {_escape_for_comment(document)} -->"


def extract(body: str | None) -> Ledger | None:
    """The ledger in a review body, or None when there is no block.

    The *last* block wins. The contract is that the ledger is the final
    thing in a review body, and an agent that quotes the previous round's
    body — which it reads on every resume — can leave an older block in the
    prose ahead of the current one. Taking the first match would then pin
    every later round to the head it already reviewed, and the loop would
    never advance past round 1.
    """
    if not body:
        return None
    matches = list(_BLOCK_RE.finditer(body))
    if not matches:
        return None
    return parse_block(matches[-1].group(1))


def latest(bodies: Iterable[str | None]) -> Ledger | None:
    """The newest ledger across thread surfaces, given newest-first.

    The block may arrive as a formal review (``gh pr review``) or as an
    issue comment (``gh pr comment``), so both surfaces are merged before
    this is called.
    """
    for body in bodies:
        ledger = extract(body)
        if ledger is not None:
            return ledger
    return None


def decide(
    ledger: Ledger | None,
    *,
    current_head: str,
    current_base: str,
    max_rounds: int | None = None,
) -> tuple[Decision, str]:
    """What this pass is. Returns the decision and a one-line reason.

    This is the table in SKILL.md's *Re-review* section, kept here so the
    watcher and the CI job cannot drift from the prose.
    """
    if ledger is None:
        return "first-pass", "no ledger in the thread; this is round 1"

    if ledger.base != current_base:
        return (
            "retargeted",
            f"base moved from {ledger.base!r} to {current_base!r}; every finding is invalidated",
        )

    if ledger.head == current_head:
        return (
            "already-reviewed",
            f"head {current_head[:8]} was already reviewed at round {ledger.round}",
        )

    if max_rounds is not None and ledger.round >= max_rounds:
        return (
            "rounds-exhausted",
            f"round {ledger.round} has reached the cap of {max_rounds}",
        )

    return (
        "resume",
        f"round {ledger.round + 1}: {len(ledger.open_findings)} open finding(s), "
        f"delta {ledger.head[:8]}..{current_head[:8]}",
    )


def touches_open_findings(ledger: Ledger, delta_files: Iterable[str]) -> bool:
    """Whether a delta can affect any finding still open.

    The cheap pre-filter. A push that touches none of these files cannot
    resolve or regress a finding, so the round can be skipped before any
    agent or graph work happens. An empty file list means the delta is
    unknown, which is treated as relevant rather than skipping silently.
    """
    files = list(delta_files)
    if not files:
        return True
    watched = ledger.open_files
    if not watched:
        return False
    return any(path in watched for path in files)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect a review-pr state ledger.")
    parser.add_argument("body", type=Path, help="File holding a review body")
    parser.add_argument(
        "--current-head", help="Current PR head SHA, for the resume decision"
    )
    parser.add_argument("--current-base", help="Current base ref")
    parser.add_argument(
        "--max-rounds", type=int, help="Cap rounds before the loop stops"
    )
    parser.add_argument(
        "--optional",
        action="store_true",
        help="Exit 0 when the body carries no ledger (exit 3 without it)",
    )
    parser.add_argument("--quiet", action="store_true", help="Print only the decision")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    try:
        body = args.body.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("could not read %s: %s", args.body, exc)
        return 2

    try:
        ledger = extract(body)
    except LedgerError as exc:
        logger.error("invalid ledger: %s", exc)
        return 3

    if ledger is None:
        if args.optional:
            return 0
        logger.error("no ledger found in %s", args.body)
        return 3

    if args.current_head and args.current_base:
        decision, reason = decide(
            ledger,
            current_head=args.current_head,
            current_base=args.current_base,
            max_rounds=args.max_rounds,
        )
        print(f"{decision}: {reason}")
    elif not args.quiet:
        print(render(ledger))
    return 0


if __name__ == "__main__":
    sys.exit(_main())

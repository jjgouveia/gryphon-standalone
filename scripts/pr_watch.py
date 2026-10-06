#!/usr/bin/env python3
"""Poll labeled PRs and decide which need a re-review round.

This is the trigger half of the ``review-pr`` re-review loop. It never
publishes anything and never runs an agent: it reads the state ledger
from each thread, applies the cheap filters, and exits non-zero when at
least one PR is ready for a round. A scheduler (Windows Task Scheduler,
cron, CI ``schedule:``) runs it; the agent invocation is a separate step.

Everything it decides is free. The SHA compare and the file-set
intersection below are why a push that cannot possibly affect an open
finding never costs a model call.

    python scripts/pr_watch.py --repo jjgouveia/gryphon-standalone --check
    python scripts/pr_watch.py --repo <o>/<r> --max-rounds 6 --json

Exit codes:
    0  no PR needs a round (the poll was quiet, which is the normal case)
    1  at least one PR is ready; with ``--json`` the list is on stdout
    2  the repository could not be queried
    3  a thread carried a ledger that could not be read
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, TypedDict

sys.path.insert(0, str(Path(__file__).parent))

from pr_ledger import (
    Finding,
    Ledger,
    LedgerError,
    decide,
    extract,
    render,
    touches_open_findings,
)

logger = logging.getLogger("pr_watch")

DEFAULT_MAX_ROUNDS = 6


class GitHubError(RuntimeError):
    """A ``gh api`` call failed or returned nothing usable."""


class Pull(TypedDict):
    """The refs a round decision needs, resolved from the pulls API."""

    number: int
    title: str
    headRefOid: str
    baseRefName: str


@dataclass(frozen=True)
class Candidate:
    number: int
    title: str
    head: str
    base: str
    round: int
    open_findings: int
    reason: str
    delta_files: tuple[str, ...] = ()
    # Which repository this PR belongs to. One poll can span several, so the
    # candidate has to say where it came from rather than relying on the
    # caller remembering which repo it asked about.
    repo: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "repo": self.repo,
            "pr": self.number,
            "title": self.title,
            "head": self.head,
            "base": self.base,
            "round": self.round,
            "open_findings": self.open_findings,
            "delta": list(self.delta_files),
            "reason": self.reason,
        }


def gh(args: list[str]) -> str:
    """Run a ``gh`` command and return stdout, or raise."""
    try:
        done = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHubError(f"gh {' '.join(args)}: {exc}") from exc
    if done.returncode != 0:
        raise GitHubError(f"gh {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def assert_repo_exists(repo: str) -> None:
    """Fail loudly on an unresolvable repository.

    ``gh pr list --json ... --label ...`` answers an unknown repository with
    exit 0 and ``[]``. Left unchecked that reads as "no PR needs a round"
    and silently stops the loop, so a mistyped repository in the scheduler
    looks exactly like a quiet afternoon.
    """
    try:
        json.loads(gh(["repo", "view", repo, "--json", "nameWithOwner"]))
    except json.JSONDecodeError as exc:
        raise GitHubError(f"{repo} did not return repository metadata: {exc}") from exc


def resolve_reviewer() -> str:
    """The account whose reviews start a watch.

    Publishing the review is the trigger, so the watcher's scope is "PRs I
    reviewed", which the search API answers with ``reviewed-by:`` without
    any label anyone has to remember to apply.
    """
    try:
        login = json.loads(gh(["api", "user", "--jq", "{login: .login}"]))
    except (GitHubError, json.JSONDecodeError) as exc:
        raise GitHubError(f"could not resolve the authenticated account: {exc}") from exc
    if not isinstance(login, dict) or not isinstance(login.get("login"), str):
        raise GitHubError("gh did not report a login for the authenticated account")
    return login["login"]


def fetch_watched_pulls(
    repo: str, *, reviewer: str, label: str | None = None
) -> list[Pull]:
    """Open, non-draft PRs this reviewer has reviewed.

    The scope is derived, never registered. A review published with
    ``--request-changes`` is the trigger, and it leaves two facts any later
    poll can read: the search index knows the reviewer's name, and the
    ledger in the thread knows which findings are still open. So a PR that
    owes its author corrections is found by asking, and there is no step
    that can be forgotten — no label to apply, no state file to drift.

    ``label`` narrows further, which is how one PR gets paused: remove it
    and the PR drops out of the scan while the rest keep running.
    """
    query = f"repo:{repo} reviewed-by:{reviewer} is:pr is:open"
    raw = gh(["api", "search/issues", "-X", "GET", "-f", f"q={query}", "-f", "per_page=100"])
    try:
        items = json.loads(raw).get("items", [])
    except json.JSONDecodeError as exc:
        raise GitHubError(f"could not parse the search result: {exc}") from exc
    if not isinstance(items, list):
        raise GitHubError("search did not return an item list")

    pulls: list[Pull] = []
    for item in items:
        if not isinstance(item, dict) or "pull_request" not in item:
            continue
        number = item.get("number")
        if not isinstance(number, int):
            continue
        if label is not None and label not in {
            entry.get("name") for entry in item.get("labels", []) if isinstance(entry, dict)
        }:
            continue

        detail = json.loads(
            gh(
                [
                    "api",
                    f"repos/{repo}/pulls/{number}",
                    "--jq",
                    "{head: .head.sha, base: .base.ref, draft: .draft}",
                ]
            )
        )
        if detail.get("draft"):
            continue
        pulls.append(
            Pull(
                number=number,
                title=item.get("title") or "",
                headRefOid=detail["head"],
                baseRefName=detail["base"],
            )
        )
    return pulls


def fetch_review_bodies(repo: str, number: int) -> list[str]:
    """Review bodies newest-first, across both surfaces.

    ``gh pr review`` lands in ``pulls/<n>/reviews``; ``gh pr comment``
    lands in ``issues/<n>/comments``. A ledger can arrive in either, so
    both are merged and the newest block wins.
    """
    bodies: list[str] = []
    for path in (f"pulls/{number}/reviews", f"issues/{number}/comments"):
        try:
            payload = json.loads(gh(["api", f"repos/{repo}/{path}", "--paginate"]))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, list):
            continue
        entries = sorted(
            (item for item in payload if isinstance(item, dict) and item.get("body")),
            key=lambda item: str(item.get("submitted_at") or item.get("created_at") or ""),
            reverse=True,
        )
        bodies.extend(str(item["body"]) for item in entries)
    return bodies


def fetch_delta_files(repo: str, number: int, since: str) -> tuple[str, ...]:
    """Files changed since the reviewed head.

    A PR with no ledger yet has no base for the comparison; returning an
    empty tuple makes the filter treat the round as relevant, which is the
    right default for a first pass.
    """
    try:
        raw = gh(
            [
                "api",
                f"repos/{repo}/pulls/{number}/files",
                "--paginate",
                "--jq",
                ".[].filename",
            ]
        )
    except GitHubError:
        return ()

    files = tuple(line.strip() for line in raw.splitlines() if line.strip())
    if not since:
        return files

    # The PR file list is the whole change, not the delta. Narrowing it needs
    # the compare API, and the endpoint is SHAs-only: `since...HEAD` resolves
    # against the local clone's HEAD, which in a scheduled poll is some other
    # branch entirely and returns an empty list. Compare against the PR's
    # current head explicitly.
    head = gh(
        ["api", f"repos/{repo}/pulls/{number}", "--jq", ".head.sha"]
    ).strip()
    try:
        compare = json.loads(
            gh(
                [
                    "api",
                    f"repos/{repo}/compare/{since}...{head}",
                    "--jq",
                    "[.files[].filename]",
                ]
            )
        )
    except (GitHubError, json.JSONDecodeError):
        return files
    if isinstance(compare, list):
        return tuple(f for f in compare if isinstance(f, str))
    return files


def newest_ledger(repo: str, number: int) -> Ledger | None:
    for body in fetch_review_bodies(repo, number):
        ledger = extract(body)
        if ledger is not None:
            return ledger
    return None


def evaluate(
    pull: Pull,
    ledger: Ledger | None,
    delta_files: tuple[str, ...],
    *,
    max_rounds: int | None,
) -> Candidate | None:
    """The round this PR needs, or None when it needs none."""
    head = pull["headRefOid"]
    base = pull["baseRefName"]
    decision, reason = decide(
        ledger, current_head=head, current_base=base, max_rounds=max_rounds
    )

    if decision == "resume" and ledger is not None:
        if not touches_open_findings(ledger, delta_files):
            return None

    if decision != "resume":
        return None

    assert ledger is not None

    # A PR whose findings are all settled is not owed anything. Without
    # this the scan would keep re-reviewing PRs the author already fixed
    # and we already confirmed.
    if not ledger.open_findings:
        return None

    return Candidate(
        number=pull["number"],
        title=pull["title"],
        head=head,
        base=base,
        round=ledger.round + 1,
        open_findings=len(ledger.open_findings),
        reason=reason,
        delta_files=delta_files,
    )


def survey(
    repo: str,
    *,
    reviewer: str | None = None,
    label: str | None = None,
    max_rounds: int | None = None,
) -> list[Candidate]:
    """Every PR that is ready for a round right now.

    A PR qualifies when this reviewer reviewed it, its ledger still carries
    an open finding — the author owes a correction — and the head has moved
    since the last round.
    """
    assert_repo_exists(repo)
    login = reviewer or resolve_reviewer()

    candidates: list[Candidate] = []
    for pull in fetch_watched_pulls(repo, reviewer=login, label=label):
        number = int(pull["number"])
        ledger = newest_ledger(repo, number)
        delta = fetch_delta_files(
            repo, number, since=ledger.head if ledger is not None else ""
        )
        candidate = evaluate(
            pull, ledger, delta, max_rounds=max_rounds
        )
        if candidate is not None:
            candidates.append(replace(candidate, repo=repo))
    return candidates


def survey_many(
    repos: Iterable[str],
    *,
    reviewer: str | None = None,
    label: str | None = None,
    max_rounds: int | None = None,
) -> list[Candidate]:
    """Survey several repositories in one poll.

    A reviewer works across repos, and the loop has to follow them: watching
    one repo while the others go unnoticed is the same failure as not
    watching at all. One repo failing does not abort the rest — a repo the
    account cannot read should not silently disable the watcher everywhere
    else — but every failure is logged, so a repo that never returns work
    is visible rather than merely quiet.
    """
    reviewer = reviewer or resolve_reviewer()
    repos = list(repos)
    candidates: list[Candidate] = []
    failures: list[GitHubError] = []
    for repo in repos:
        try:
            candidates.extend(
                survey(
                    repo,
                    reviewer=reviewer,
                    label=label,
                    max_rounds=max_rounds,
                )
            )
        except GitHubError as exc:
            logger.warning("skipping %s: %s", repo, exc)
            failures.append(exc)

    # If nothing could be read, the poll knows nothing and must not answer
    # "nothing to do": a mistyped or unreachable repo list would otherwise
    # look exactly like a quiet afternoon, which is the failure this whole
    # module keeps having to avoid. A partial failure still continues,
    # because one unreadable repo should not disable the rest.
    if failures and len(failures) == len(repos):
        raise failures[-1]
    return candidates


def render_round_body(ledger: Ledger, verdicts: Iterable[Finding], prose: str) -> str:
    """A round body: the review prose, then the ledger for the next pass.

    One helper so the CI job and the skill emit the same shape, and so a
    round can never be published without the ledger that makes the next
    round resumable.
    """
    lines = [prose.rstrip(), ""]
    for finding in sorted(verdicts, key=lambda f: f.id):
        lines.append(f"{finding.id}. {finding.title} — {finding.status}")
    lines += ["", render(ledger), ""]
    return "\n".join(lines)


def escalate(previous: Ledger | None, verdicts: Iterable[Finding]) -> str:
    """The flag an unattended round is allowed to publish.

    A ``high`` finding that the previous round already raised and that is
    still open at the new head is a disconfirmation: the author was told,
    pushed, and the blocker is verifiably still there. A human reviewer
    blocks on that without re-deriving the analysis. Anything else is a
    comment, because the bot's first disagreement with new code is an
    opinion, and a merge blocked on an opinion costs more than one missed.

    ``previous`` supplies the carried ids. Deriving that here rather than
    asking the agent for it is deliberate: a field the model can forget to
    set would silently downgrade every escalation to a comment, which is
    the failure that looks like the system working.

    Never ``approve`` — an unattended approval can merge a PR nobody read.
    """
    carried = {f.id for f in previous.open_findings} if previous is not None else set()
    for finding in verdicts:
        if (
            finding.sev == "high"
            and finding.status == "open"
            and finding.id in carried
        ):
            return "request-changes"
    return "comment"


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report which labeled PRs need a re-review round."
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="owner/name, or several separated by commas",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="Only consider PRs carrying this label (default: every PR you reviewed)",
    )
    parser.add_argument(
        "--reviewer",
        default=None,
        help="Whose reviews start a watch (default: the authenticated account)",
    )
    parser.add_argument(
        "--max-rounds", type=int, help="Stop after this many rounds per PR"
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON on stdout")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print nothing; exit 1 when a PR is ready (for a scheduler hook)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    repos = [part.strip() for part in args.repo.split(",") if part.strip()]

    try:
        candidates = survey_many(
            repos,
            reviewer=args.reviewer,
            label=args.label,
            max_rounds=args.max_rounds,
        )
    except GitHubError as exc:
        logger.error("%s", exc)
        return 2
    except LedgerError as exc:
        logger.error(
            "a thread carried a ledger that could not be read: %s", exc
        )
        return 3

    if not candidates:
        return 0

    if args.json:
        print(json.dumps([c.as_dict() for c in candidates], indent=2))
    elif not args.check:
        for c in candidates:
            print(
                f"PR #{c.number} round {c.round}: {c.open_findings} open "
                f"finding(s) — {c.reason}"
            )

    return 1


if __name__ == "__main__":
    sys.exit(_main())

#!/usr/bin/env python3
"""Run re-review rounds on this machine, on a schedule.

This is the local half of the loop. ``pr_watch.py`` decides *whether* a
round is needed — free, no model — and this runner invokes the agent to
run it. Windows Task Scheduler calls it on a timer; nothing here needs to
be a daemon.

Unlike the CI path, the agent publishes the review itself: it runs with the
user's own ``gh`` credentials, so the review is attributed to the reviewer
rather than to ``github-actions[bot]``. That also means this machine's
checkout has to be usable — a working tree, a virtualenv, the graph. The
CI runner has none of that, so the local round can run the tests in the
skill's step 4 and the CI round cannot.

Three things this guards:

* **One round at a time.** Task Scheduler will happily start a second
  instance while the first is still running, and two agents reviewing the
  same PR post two reviews for one head. A lock file keyed on the process
  id keeps that from happening; a lock left by a killed process is
  reclaimed rather than blocking the loop forever.
* **The round is for the head that was surveyed.** The head can move while
  the agent works. That is not fatal — the ledger records what was
  reviewed and the next round picks up the new head — but it is logged,
  because a poll that keeps landing on superseded heads means the author
  is pushing faster than a round takes.
* **The published flag matches policy.** After the round, the newest ledger
  is read back and checked against ``pr_watch.escalate``. An agent that
  published ``request-changes`` without a carried high blocker ignored the
  rule; reporting that is better than leaving a merge blocked on a
  violation nobody notices.

    python scripts/review_watch.py --repo <owner>/<name> --dry-run
    python scripts/review_watch.py --repo <owner>/<name> --max-rounds 6
    python scripts/review_watch.py --register --repo <owner>/<name>

Exit codes:
    0  poll completed; nothing needed a round, or every round succeeded
    1  at least one round failed
    2  the repository could not be queried
    3  the agent could not be found
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pr_ledger import Ledger
from pr_watch import Candidate, GitHubError, escalate, newest_ledger, survey

logger = logging.getLogger("review_watch")

LOCK_NAME = "review_watch.lock"
TASK_NAME = "gryphon-review-watch"
DEFAULT_INTERVAL_MINUTES = 15
AGENT_TIMEOUT_SECONDS = 3600


@dataclass
class RoundResult:
    pr: int
    head: str
    status: str
    detail: str = ""
    expected_flag: str = ""
    published_flag: str = ""
    carried: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        # A dry run reported a candidate without doing anything, which is
        # not a failure; counting it as one would have a scheduled poll
        # report a broken task every time it found work to do.
        return self.status not in ("published", "unchanged", "dry-run")


# --- the lock ------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """Whether a process id is still running, on Windows and elsewhere."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            done = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return True
        return str(pid) in done.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Lock:
    """Single-instance guard for one repository.

    The lock is a file holding the owning pid. It is taken exclusively so
    two pollers racing cannot both win, and it is reclaimed when the pid
    inside is gone — a scheduler that killed the process mid-round must
    not wedge the loop permanently, which is the failure that looks like
    "nothing to review" forever.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def _holder(self) -> int | None:
        try:
            return int(self.path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def take(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = self._holder()
            if holder is not None and pid_alive(holder):
                return False
            logger.warning("reclaiming a stale lock from pid %s", holder)
            try:
                self.path.unlink()
            except OSError:
                return False
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        self.acquired = True
        return True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except OSError:
            logger.warning("could not remove the lock at %s", self.path)
        self.acquired = False

    def __enter__(self) -> "Lock":
        if not self.take():
            raise LockBusyError(f"another round is already running (pid {self._holder()})")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class LockBusyError(RuntimeError):
    """Another poll is already running, so this one must not proceed."""


# --- the agent -----------------------------------------------------------


def build_prompt(candidate: Candidate, repo_root: Path) -> str:
    return (
        f"Run one re-review round on PR #{candidate.number} in the repository "
        f"at {repo_root}.\n\n"
        "Invoke the review-pr skill with --resume. Read the skill first; it "
        "carries the procedure and the ledger contract.\n\n"
        f"The round is for head {candidate.head} (round {candidate.round}), "
        f"based on {candidate.base}. The previous round left "
        f"{candidate.open_findings} open finding(s). Files changed since then: "
        f"{', '.join(candidate.delta_files) or 'unknown'}.\n\n"
        "This round is unattended: there is no human to answer a question, so "
        "decide and proceed. Follow the \"Unattended rounds\" section for which "
        "review flag you may publish. You may never approve.\n\n"
        "Publish the review yourself with gh, using the ledger from the skill to "
        "keep the next round resumable. Post it as the repository's configured "
        "reviewer; do not leave the review in a draft."
    )


def agent_command(prompt: str, agent: str) -> list[str]:
    if agent == "opencode":
        return ["opencode", "run", prompt]
    return ["claude", "-p", prompt]


def run_round(
    repo: str,
    candidate: Candidate,
    *,
    repo_root: Path,
    agent: str,
    dry_run: bool,
) -> RoundResult:
    """One round, end to end, with the published flag checked afterwards."""
    # Read before the agent runs. After the round the thread's newest ledger
    # is the new one, so this is the only chance to capture the carried set
    # the escalation policy judges against.
    previous = newest_ledger(repo, candidate.number)

    if dry_run:
        return RoundResult(
            pr=candidate.number,
            head=candidate.head,
            status="dry-run",
            detail=(
                f"{len(previous.open_findings)} finding(s) carried from round "
                f"{previous.round}" if previous else "no ledger in the thread"
            ),
            carried=len(previous.open_findings) if previous else 0,
        )

    command = agent_command(build_prompt(candidate, repo_root), agent)
    try:
        done = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=AGENT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise
    except subprocess.TimeoutExpired:
        return RoundResult(
            pr=candidate.number,
            head=candidate.head,
            status="timeout",
            detail=f"no result after {AGENT_TIMEOUT_SECONDS}s",
        )

    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-3:]
        return RoundResult(
            pr=candidate.number,
            head=candidate.head,
            status="agent-failed",
            detail="; ".join(tail)[-400:],
        )

    return check_published(repo, candidate, previous)


def check_published(
    repo: str, candidate: Candidate, previous: Ledger | None
) -> RoundResult:
    """Read the thread back and confirm what actually landed.

    The allowed flag is only knowable now: ``escalate`` needs the
    verdicts, and the verdicts are whatever the agent just published.
    Judging it before the round would mean judging an empty list.
    """
    result = RoundResult(pr=candidate.number, head=candidate.head, status="unchanged")
    try:
        latest = newest_ledger(repo, candidate.number)
    except GitHubError as exc:
        result.status = "unverified"
        result.detail = str(exc)
        return result

    if latest is None:
        result.status = "unverified"
        result.detail = "no ledger found in the thread after the round"
        return result

    result.status = "published"
    result.published_flag = latest.flag
    result.expected_flag = escalate(previous, latest.findings)
    result.carried = len(previous.open_findings) if previous is not None else 0

    if latest.head != candidate.head:
        result.notes.append(
            f"the round reviewed {latest.head[:8]}, but the PR head is now "
            f"{candidate.head[:8]}; the next poll picks the new head up"
        )

    if latest.flag == "request-changes" and result.expected_flag != "request-changes":
        result.notes.append(
            "policy violation: published request-changes with no high finding "
            "carried over from a previous round"
        )
    return result


# --- the poll ------------------------------------------------------------


def poll(
    repo: str,
    *,
    repo_root: Path,
    agent: str,
    max_rounds: int | None,
    dry_run: bool,
    lock_dir: Path,
) -> list[RoundResult]:
    lock = Lock(lock_dir / LOCK_NAME)
    with lock:
        candidates = survey(repo, max_rounds=max_rounds)
        results: list[RoundResult] = []
        for candidate in candidates:
            logger.info(
                "PR #%s round %s: %s", candidate.number, candidate.round, candidate.reason
            )
            results.append(
                run_round(
                    repo,
                    candidate,
                    repo_root=repo_root,
                    agent=agent,
                    dry_run=dry_run,
                )
            )
            # One round at a time on purpose: a second agent would read the
            # thread the first one has not finished writing.
            time.sleep(2)
        return results


def register_task(
    repo_root: Path,
    repo: str,
    *,
    interval_minutes: int,
    max_rounds: int | None,
    log_path: Path,
) -> None:
    """Register the poll in Windows Task Scheduler, at logon."""
    if os.name != "nt":
        raise RuntimeError("Task Scheduler registration is Windows-only")

    repo_root.mkdir(parents=True, exist_ok=True)
    runner = Path(__file__).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-Command",
        (
            f"& '{sys.executable}' '{runner}' --repo '{repo}' "
            f"--repo-root '{repo_root}' --max-rounds {max_rounds or 6} "
            f">> '{log_path}' 2>&1"
        ),
    ]

    subprocess.run(
        [
            "schtasks.exe",
            "/Create",
            "/F",
            "/SC",
            "ONLOGON",
            "/TN",
            TASK_NAME,
            "/RL",
            "LIMITED",
            "/TR",
            subprocess.list2cmdline(command),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    interval = max(1, interval_minutes)
    subprocess.run(
        [
            "schtasks.exe",
            "/Change",
            "/TN",
            TASK_NAME,
            "/SC",
            "MINUTE",
            "/MO",
            str(interval),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    print(f"Registered {TASK_NAME}: every {interval} minute(s), from logon.")
    print(f"Logs: {log_path}")
    print(f"Pause one PR: remove the 'gryphon:watch' label. Disable all: "
          f"schtasks /Delete /TN {TASK_NAME} /F")


def unregister_task() -> None:
    subprocess.run(
        ["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"],
        check=True,
        capture_output=True,
        text=True,
    )
    print(f"Removed {TASK_NAME}.")


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run unattended re-review rounds for labeled PRs."
    )
    parser.add_argument("--repo", help="owner/name")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Checkout the agent runs in (default: the current directory)",
    )
    parser.add_argument(
        "--agent",
        choices=("claude", "opencode"),
        default="claude",
        help="Headless agent to invoke",
    )
    parser.add_argument("--max-rounds", type=int, default=6)
    parser.add_argument("--dry-run", action="store_true", help="Report, do not run")
    parser.add_argument(
        "--lock-dir",
        type=Path,
        default=Path.home() / ".gryphon",
        help="Where the single-instance lock lives",
    )
    parser.add_argument("--register", action="store_true", help="Register a scheduled task")
    parser.add_argument("--unregister", action="store_true", help="Remove the task")
    parser.add_argument("--interval-minutes", type=int, default=DEFAULT_INTERVAL_MINUTES)
    parser.add_argument("--log", type=Path, default=Path.home() / ".gryphon" / "review-watch.log")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.unregister:
        unregister_task()
        return 0
    if args.register:
        if not args.repo:
            parser.error("--register needs --repo")
        register_task(
            args.repo_root.resolve(),
            args.repo,
            interval_minutes=args.interval_minutes,
            max_rounds=args.max_rounds,
            log_path=args.log,
        )
        return 0
    if not args.repo:
        parser.error("--repo is required to poll")

    repo_root = args.repo_root.resolve()
    if not (repo_root / "skills" / "review-pr" / "SKILL.md").is_file():
        logger.error(
            "%s does not look like the gryphon checkout: skills/review-pr/SKILL.md "
            "is missing, so the agent would find no skill to run",
            repo_root,
        )
        return 2

    try:
        results = poll(
            args.repo,
            repo_root=repo_root,
            agent=args.agent,
            max_rounds=args.max_rounds,
            dry_run=args.dry_run,
            lock_dir=args.lock_dir,
        )
    except LockBusyError as exc:
        logger.info("%s", exc)
        return 0
    except GitHubError as exc:
        logger.error("%s", exc)
        return 2
    except FileNotFoundError as exc:
        logger.error(
            "the %s agent is not on PATH (%s); pass --agent to choose another",
            args.agent,
            exc,
        )
        return 3

    failed = [r for r in results if r.failed]
    for result in results:
        marker = "!" if result.failed else " "
        print(
            f"{marker} PR #{result.pr} {result.status}"
            + (f" [{result.expected_flag} -> {result.published_flag}]"
               if result.published_flag else "")
        )
        for note in result.notes:
            print(f"    {note}")
    if not results:
        logger.info("nothing needed a round")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())

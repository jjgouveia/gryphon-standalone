#!/usr/bin/env python3
"""Resume a review when the author pushes, inside the open session.

Claude Code fires this hook at three points where a pending round is worth
mentioning, and nowhere else:

``Stop``
    The agent finished a turn. This is the case the loop was built for: the
    developer pushed while you were reading, and the round continues without
    anyone typing.
``SessionStart``
    You came back to the terminal. Whatever landed while the session was
    closed is waiting, and this is the moment to say so.
``UserPromptSubmit``
    You asked something. Catches a push that landed between turns.

The design point is that this is *not* a push notification. Nothing runs
while you are away and nothing posts under your name while you sleep. The
round happens in the session you already have open, where you can watch it,
interrupt it, or redirect it. What it removes is the remembering: you no
longer have to poll GitHub to find out the developer replied.

Registered by the ``review-pr`` skill rather than by a settings file, so it
only exists in sessions where you asked for a review. Claude Code keeps a
skill's hooks for the rest of the session, including on later turns.

The scope is derived, never registered: a review published with
``--request-changes`` left the search index knowing you reviewed the PR and
the thread's ledger knowing which findings are still open, so a poll finds
its own work.

Protocol
--------
Exit 0 with no output: nothing to do.

Exit 0 with ``hookSpecificOutput.additionalContext``: here is the pending
round. This is the non-error channel — the transcript labels it "Stop hook
feedback" — and it is the only channel all three events share.

Never blocks. Exit 2 would route as a blocking decision and show a hook
error, which would be wrong for something this ordinary.

Latency matters more than it looks. ``UserPromptSubmit`` blocks model
processing until the hook returns, and a hook that times out there has its
``additionalContext`` discarded — so a slow poll does not degrade the
feature, it silently removes it. The throttle is therefore shared across all
three events: one poll per interval, whichever event happened to arrive
first.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pr_watch import GitHubError, survey_many

logger = logging.getLogger("pr_watch_hook")

# Only these events accept the context channel this hook uses. Injecting
# elsewhere would be dropped, or worse, block a turn.
INFORMING_EVENTS = ("Stop", "SessionStart", "UserPromptSubmit")

# One poll per turn is a couple of API calls; the floor keeps a long session
# with many turns from turning into a busy loop. Shared across events so
# three events inside a minute cost one poll, not three.
DEFAULT_MIN_INTERVAL = 45.0

ENV_REPO = "GRYPHON_PR_WATCH_REPO"
ENV_MAX_ROUNDS = "GRYPHON_PR_WATCH_MAX_ROUNDS"
ENV_INTERVAL = "GRYPHON_PR_WATCH_INTERVAL"
ENV_STATE = "GRYPHON_PR_WATCH_STATE"


def state_path() -> Path:
    override = os.environ.get(ENV_STATE)
    if override:
        return Path(override)
    session = os.environ.get("CLAUDE_SESSION_ID", "default")
    base = Path(os.environ.get("TEMP") or "/tmp") / "gryphon-pr-watch"
    return base / f"{session}.json"


def read_event() -> dict:
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def last_poll(path: Path) -> float:
    try:
        return float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0.0


def mark_poll(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        logger.debug("could not record the poll time at %s", path)


def pending_repos(event: dict | None = None) -> list[str]:
    """The repositories to poll, from the environment or where the work is.

    The default is the checkout the session is working in, which is what
    "the place the review is happening" means and is why this needs no
    configuration: a session in the cvld checkout watches cvld, and one in
    the gryphon checkout watches gryphon, with nothing to set up per repo.

    ``cwd`` comes from the hook's own input, not from this process. Claude
    Code reports the worktree root after the agent enters a worktree and
    the new directory after it runs a ``cd``, so the watcher follows the
    agent rather than the directory the hook happened to be spawned in.

    ``GRYPHON_PR_WATCH_REPO`` overrides with a comma-separated list, for
    the case where a repo is reviewed without being checked out.
    """
    raw = os.environ.get(ENV_REPO, "")
    repos = [part.strip() for part in raw.split(",") if part.strip()]
    if repos:
        return repos

    start = None
    if event:
        value = event.get("cwd")
        if isinstance(value, str) and value:
            start = Path(value)

    discovered = discover_repo(start)
    return [discovered] if discovered else []


def discover_repo(start: Path | None = None) -> str | None:
    """The GitHub owner/name of a checkout's ``origin``.

    ``-C`` points git at the directory the session is in, so a worktree or
    a subdirectory both resolve to the same repository.
    """
    command = ["git"]
    if start is not None:
        command += ["-C", str(start)]
    command += ["remote", "get-url", "origin"]

    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return repo_from_remote(done.stdout.strip())


def repo_from_remote(url: str) -> str | None:
    """Turn a remote URL into owner/name, or None when it is not GitHub."""
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@") and ":" in url:
        return url.split(":", 1)[1]
    if "github.com/" in url:
        return url.split("github.com/", 1)[1].strip("/")
    return None


def build_context(candidates: list) -> str:
    """The instruction handed back to the agent."""
    def where(candidate) -> str:
        return f"{candidate.repo}#{candidate.number}" if candidate.repo else f"#{candidate.number}"

    if len(candidates) == 1:
        first = candidates[0]
        opening = (
            f"{where(first)} got a push since your last review round "
            f"(head {first.head[:8]}, round {first.round}, base {first.base})."
        )
    else:
        listing = "; ".join(
            f"{where(c)} head {c.head[:8]} round {c.round}" for c in candidates
        )
        opening = (
            f"{len(candidates)} reviewed PRs got pushes since their last round: "
            f"{listing}."
        )

    lines = [
        opening,
        "",
        "Continue the review in this session. Run the review-pr skill with "
        "--resume on each of them: the ledger in the thread has the round "
        "number, the last verified head and the open findings, so there is "
        "nothing to reconstruct.",
        "",
        "If you have already reported this round to the user and they have "
        "moved on, say which PRs are pending and stop rather than starting a "
        "full round unprompted.",
    ]
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")

    event = read_event()
    name = event.get("hook_event_name")

    # Claude Code sets this while it is already continuing because of a Stop
    # hook. Re-asking here would ask the same question about the same heads
    # and loop, so stay quiet until the next real turn boundary.
    if event.get("stop_hook_active"):
        return 0

    if name not in INFORMING_EVENTS:
        return 0

    repo = pending_repos(event)
    if not repo:
        return 0

    try:
        interval = float(os.environ.get(ENV_INTERVAL, DEFAULT_MIN_INTERVAL))
    except ValueError:
        interval = DEFAULT_MIN_INTERVAL
    marker = state_path()
    now = time.time()
    if now - last_poll(marker) < interval:
        return 0
    mark_poll(marker)

    try:
        max_rounds = int(os.environ.get(ENV_MAX_ROUNDS, "6"))
    except ValueError:
        max_rounds = 6

    try:
        candidates = survey_many(repo, max_rounds=max_rounds)
    except GitHubError as exc:
        # A poll that cannot reach GitHub says nothing; never claim the loop
        # is up to date when the check itself failed.
        logger.error("pr poll failed for %s: %s", repo, exc)
        return 0

    if not candidates:
        return 0

    payload = {
        "hookSpecificOutput": {
            "hookEventName": name,
            "additionalContext": build_context(candidates),
        }
    }
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
